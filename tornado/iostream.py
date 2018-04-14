#!/usr/bin/env python
#
# Copyright 2009 Facebook
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""Utility classes to write to and read from non-blocking files and sockets.

Contents:

* `BaseIOStream`: Generic interface for reading and writing.
* `IOStream`: Implementation of BaseIOStream using non-blocking sockets.
* `SSLIOStream`: SSL-aware version of IOStream.
* `PipeIOStream`: Pipe-based IOStream implementation.
"""

from __future__ import absolute_import, division, print_function, with_statement

import collections
import errno
import numbers
import os
import socket
import ssl
import sys
import re

from tornado import ioloop
from tornado.log import gen_log, app_log
from tornado.netutil import ssl_wrap_socket, ssl_match_hostname, SSLCertificateError
from tornado import stack_context
from tornado.util import bytes_type

try:
    from tornado.platform.posix import _set_nonblocking
except ImportError:
    _set_nonblocking = None

# 对于 IOStream，整体的认识就是，它负责IO读写，顺便回调。

# 综上述，当上层调用iostream的read_*方法时，
# 它首先设置回调，
# 然后调用_try_inline_read进行非阻塞式读取，能一次性读到满足条件最好，不行就监听read。
# 当read事件发生时再调用handle_read会做好善后处理（在上一段）。
# 整个过程不会被阻塞，就是回调里跳来跳去的可能会花点时间（和网络延迟比起来简直不值一提）。
# 同理，write函数也是这样，把内容放进自身缓冲区，当write事件到来时再输出，省去了网络延迟。


class StreamClosedError(IOError):
    """Exception raised by `IOStream` methods when the stream is closed.

    Note that the close callback is scheduled to run *after* other
    callbacks on the stream (to allow for buffered data to be processed),
    so you may see this error before you see the close callback.
    """
    pass


class BaseIOStream(object):
    """A utility class to write to and read from a non-blocking file or socket.

    We support a non-blocking ``write()`` and a family of ``read_*()`` methods.
    All of the methods take callbacks (since writing and reading are
    non-blocking and asynchronous).

    When a stream is closed due to an error, the IOStream's ``error``
    attribute contains the exception object.

    Subclasses must implement `fileno`, `close_fd`, `write_to_fd`,
    `read_from_fd`, and optionally `get_fd_error`.
    """
    def __init__(self, io_loop=None, max_buffer_size=None,
                 read_chunk_size=4096):

        # 非阻塞读写基类BaseIOStream。
        # 首先是__init__方法，记录了ioloop（毕竟要实现异步非阻塞，ioloop是必须的），
        # 然后初始化了两个缓冲区【双端队列】和缓冲区大小：
        self.io_loop = io_loop or ioloop.IOLoop.current()
        self.max_buffer_size = max_buffer_size or 104857600
        self.read_chunk_size = read_chunk_size
        self.error = None
        self._read_buffer = collections.deque()
        self._write_buffer = collections.deque()

        # 以及将其它标志置为默认none。很明显，io的最底层就是缓冲区了，
        # 先来看关于读缓冲区的两个方法: _read_to_buffer 和 _read_from_buffer
        # 外加一个_consume方法。
        self._read_buffer_size = 0
        self._write_buffer_frozen = False
        self._read_delimiter = None
        self._read_regex = None
        self._read_bytes = None
        self._read_until_close = False
        self._read_callback = None
        self._streaming_callback = None
        self._write_callback = None
        self._close_callback = None
        self._connect_callback = None
        self._connecting = False
        self._state = None
        self._pending_callbacks = 0
        self._closed = False

    def fileno(self):
        """Returns the file descriptor for this stream."""
        raise NotImplementedError()

    def close_fd(self):
        """Closes the file underlying this stream.

        ``close_fd`` is called by `BaseIOStream` and should not be called
        elsewhere; other users should call `close` instead.
        """
        raise NotImplementedError()

    def write_to_fd(self, data):
        """Attempts to write ``data`` to the underlying file.

        Returns the number of bytes written.
        """
        raise NotImplementedError()

    def read_from_fd(self):
        """Attempts to read from the underlying file.

        Returns ``None`` if there was nothing to read (the socket
        returned `~errno.EWOULDBLOCK` or equivalent), otherwise
        returns the data.  When possible, should return no more than
        ``self.read_chunk_size`` bytes at a time.
        """
        raise NotImplementedError()

    def get_fd_error(self):
        """Returns information about any error on the underlying file.

        This method is called after the `.IOLoop` has signaled an error on the
        file descriptor, and should return an Exception (such as `socket.error`
        with additional information, or None if no such information is
        available.
        """
        return None

    def read_until_regex(self, regex, callback):
        """Run ``callback`` when we read the given regex pattern.

        The callback will get the data read (including the data that
        matched the regex and anything that came before it) as an argument.
        """
        self._set_read_callback(callback)
        self._read_regex = re.compile(regex)
        self._try_inline_read()

    def read_until(self, delimiter, callback):
        """Run ``callback`` when we read the given delimiter.

        The callback will get the data read (including the delimiter)
        as an argument.
        """

        # read_until和read_bytes是最常用的[读]接口，
        # 它们工作的过程【都】是：
        # 1.先注册读事件结束时调用的回调函数，
        # 2.然后调用_try_inline_read方法

        # 回调函数，即：HTTPConnection的 _on_headers 方法
        self._set_read_callback(callback)
        # 终止界定 \r\n\r\n
        self._read_delimiter = delimiter
        self._try_inline_read()

    def read_bytes(self, num_bytes, callback, streaming_callback=None):
        """Run callback when we read the given number of bytes.

        If a ``streaming_callback`` is given, it will be called with chunks
        of data as they become available, and the argument to the final
        ``callback`` will be empty.  Otherwise, the ``callback`` gets
        the data as an argument.
        """
        self._set_read_callback(callback)
        assert isinstance(num_bytes, numbers.Integral)
        self._read_bytes = num_bytes
        self._streaming_callback = stack_context.wrap(streaming_callback)
        self._try_inline_read()

    def read_until_close(self, callback, streaming_callback=None):
        """Reads all data from the socket until it is closed.

        If a ``streaming_callback`` is given, it will be called with chunks
        of data as they become available, and the argument to the final
        ``callback`` will be empty.  Otherwise, the ``callback`` gets the
        data as an argument.

        Subject to ``max_buffer_size`` limit from `IOStream` constructor if
        a ``streaming_callback`` is not used.
        """
        # read_until_close主要用于IOStream流关闭前后的读取：
        # 如果调用read_until_close时
        # stream已经关闭，那么将会_consume掉_read_buffer中的所有数据；
        # 否则_read_until_close标志位设为True，注册_streaming_callback回调函数，
        # 调用_add_io_state添加io_loop.READ状态。

        self._set_read_callback(callback)
        self._streaming_callback = stack_context.wrap(streaming_callback)
        if self.closed():
            if self._streaming_callback is not None:
                self._run_callback(self._streaming_callback,
                                   self._consume(self._read_buffer_size))
            self._run_callback(self._read_callback,
                               self._consume(self._read_buffer_size))
            self._streaming_callback = None
            self._read_callback = None
            return
        self._read_until_close = True
        self._streaming_callback = stack_context.wrap(streaming_callback)
        self._try_inline_read()

    def write(self, data, callback=None):
        """Write the given data to this stream.

        If ``callback`` is given, we call it when all of the buffered write
        data has been successfully written to the stream. If there was
        previously buffered write data and an old write callback, that
        callback is simply overwritten with this new callback.
        """

        # write首先将data按照数据块大小WRITE_BUFFER_CHUNK_SIZE分块写入write_buffer，
        # 然后调用handle_write向socket发送数据。
        assert isinstance(data, bytes_type)
        self._check_closed()
        # We use bool(_write_buffer) as a proxy for write_buffer_size>0,
        # so never put empty strings in the buffer.
        if data:
            # Break up large contiguous strings before inserting them in the
            # write buffer, so we don't have to recopy the entire thing
            # as we slice off pieces to send to the socket.
            WRITE_BUFFER_CHUNK_SIZE = 128 * 1024
            if len(data) > WRITE_BUFFER_CHUNK_SIZE:
                for i in range(0, len(data), WRITE_BUFFER_CHUNK_SIZE):
                    self._write_buffer.append(data[i:i + WRITE_BUFFER_CHUNK_SIZE])
            else:
                self._write_buffer.append(data)
        # 将数据保存到collections.deque()类型的双向队列中_write_buffer中
        # HTTPConnection(object) 的write方法中也有这句：
        # self._write_callback = stack_context.wrap(callback)

        self._write_callback = stack_context.wrap(callback)
        if not self._connecting:
            self._handle_write()
            if self._write_buffer:
                self._add_io_state(self.io_loop.WRITE)
            self._maybe_add_error_listener()

    def set_close_callback(self, callback):
        """Call the given callback when the stream is closed."""
        self._close_callback = stack_context.wrap(callback)

    def close(self, exc_info=False):
        """Close this stream.

        If ``exc_info`` is true, set the ``error`` attribute to the current
        exception from `sys.exc_info` (or if ``exc_info`` is a tuple,
        use that instead of `sys.exc_info`).
        """
        if not self.closed():
            if exc_info:
                if not isinstance(exc_info, tuple):
                    exc_info = sys.exc_info()
                if any(exc_info):
                    self.error = exc_info[1]
            if self._read_until_close:
                if (self._streaming_callback is not None and
                        self._read_buffer_size):
                    self._run_callback(self._streaming_callback,
                                       self._consume(self._read_buffer_size))
                callback = self._read_callback
                self._read_callback = None
                self._read_until_close = False
                self._run_callback(callback,
                                   self._consume(self._read_buffer_size))
            if self._state is not None:
                self.io_loop.remove_handler(self.fileno())
                self._state = None
            self.close_fd()
            self._closed = True
        self._maybe_run_close_callback()

    def _maybe_run_close_callback(self):
        if (self.closed() and self._close_callback and
                self._pending_callbacks == 0):
            # if there are pending callbacks, don't run the close callback
            # until they're done (see _maybe_add_error_handler)
            cb = self._close_callback
            self._close_callback = None
            self._run_callback(cb)
            # Delete any unfinished callbacks to break up reference cycles.
            self._read_callback = self._write_callback = None

    def reading(self):
        """Returns true if we are currently reading from the stream."""
        return self._read_callback is not None

    def writing(self):
        """Returns true if we are currently writing to the stream."""
        return bool(self._write_buffer)

    def closed(self):
        """Returns true if the stream has been closed."""
        return self._closed

    def set_nodelay(self, value):
        """Sets the no-delay flag for this stream.

        By default, data written to TCP streams may be held for a time
        to make the most efficient use of bandwidth (according to
        Nagle's algorithm).  The no-delay flag requests that data be
        written as soon as possible, even if doing so would consume
        additional bandwidth.

        This flag is currently defined only for TCP-based ``IOStreams``.

        .. versionadded:: 3.1
        """
        pass

    def _handle_events(self, fd, events):
        # 单例模式创建IOLoop对象，然后将socket对象句柄作为key，
        # 被封装了的函数【_handle_events】作为value，添加到IOLoop对象的【_handlers】字段中
        # 通常为IOLoop对象add_handler方法传入的回调函数，由IOLoop的事件机制来进行调度。

        # _handle_events主要是负责【事件的分发】，
        # 将发生的事件和和read/write进行比对并调用响应的回调，
        # 同时检查自身状态机的状态（是否在读，是否在写，是否已经关闭等）向ioloop注册新的回调
        # (这个函数有点像memcached里的超级状态机drive_machine，不过很明显tornado比它简单多了
        # （tornado的状态机挺弱的，我觉得）。
        # 相应的handle_read和handle_write被调用，
        # handle_read就是调用了read_to_buffer把内容复制进读缓冲区并调用
        # read_from_buffer在条件被满足时执行回调，
        # handle_write就调用write_to_fd 把写缓冲区中的内容移除

        # 本函数作用： 将处理之后的响应数据发送给客户端

        if self.closed():
            gen_log.warning("Got events for closed stream %d", fd)
            return
        try:
            # #由于在 2.23 步骤中已经将epoll的状态更新为READ，所以这次会执行_handle_read方法
            if events & self.io_loop.READ:
                self._handle_read()
            # 执行完_handle_read后，客户端socket被关闭且置空，所有此处就会执行return
            if self.closed():
                return
            # ===============================终止===========================
            if events & self.io_loop.WRITE:
                if self._connecting:
                    self._handle_connect()
                # 执行_handle_write方法，内部调用socket.send将数据响应给客户端
                self._handle_write()
            if self.closed():
                return
            if events & self.io_loop.ERROR:
                self.error = self.get_fd_error()
                # We may have queued up a user callback in _handle_read or
                # _handle_write, so don't close the IOStream until those
                # callbacks have had a chance to run.
                self.io_loop.add_callback(self.close)
                return
            state = self.io_loop.ERROR
            if self.reading():
                state |= self.io_loop.READ
            if self.writing():
                state |= self.io_loop.WRITE
            if state == self.io_loop.ERROR:
                state |= self.io_loop.READ
            if state != self._state:
                assert self._state is not None, \
                    "shouldn't happen: _handle_events without self._state"
                self._state = state
                self.io_loop.update_handler(self.fileno(), self._state)
        except Exception:
            gen_log.error("Uncaught exception, closing connection.",
                          exc_info=True)
            self.close(exc_info=True)
            raise

    def _run_callback(self, callback, *args):
        def wrapper():
            self._pending_callbacks -= 1
            try:
                callback(*args)
            except Exception:
                app_log.error("Uncaught exception, closing connection.",
                              exc_info=True)
                # Close the socket on an uncaught exception from a user callback
                # (It would eventually get closed when the socket object is
                # gc'd, but we don't want to rely on gc happening before we
                # run out of file descriptors)
                self.close(exc_info=True)
                # Re-raise the exception so that IOLoop.handle_callback_exception
                # can see it and log the error
                raise
            self._maybe_add_error_listener()
        # We schedule callbacks to be run on the next IOLoop iteration
        # rather than running them directly for several reasons:
        # * Prevents unbounded stack growth when a callback calls an
        #   IOLoop operation that immediately runs another callback
        # * Provides a predictable execution context for e.g.
        #   non-reentrant mutexes
        # * Ensures that the try/except in wrapper() is run outside
        #   of the application's StackContexts
        with stack_context.NullContext():
            # stack_context was already captured in callback, we don't need to
            # capture it again for IOStream's wrapper.  This is especially
            # important if the callback was pre-wrapped before entry to
            # IOStream (as in HTTPConnection._header_callback), as we could
            # capture and leak the wrong context here.
            self._pending_callbacks += 1
            self.io_loop.add_callback(wrapper)

    def _handle_read(self):
        try:
            try:
                # Pretend to have a pending callback so that an EOF in
                # _read_to_buffer doesn't trigger an immediate close
                # callback.  At the end of this method we'll either
                # estabilsh a real pending callback via
                # _read_from_buffer or run the close callback.
                #
                # We need two try statements here so that
                # pending_callbacks is decremented before the `except`
                # clause below (which calls `close` and does need to
                # trigger the callback)

                # 假装有一个挂起的回调函数，以便在
                # _read_to_buffer不会触发立即关闭
                # 回调。在这个方法的结尾，我们要么
                # 建立一种真正的等待回调通过
                # _read_from_buffer或运行接近回调。
                # 我们这里需要两个尝试语句，以便
                # pending_callbacks递减在
                # `除了`下面的条款（称为“结束”），确实需要触发回调）

                # 等待回调
                self._pending_callbacks += 1
                while not self.closed():
                    # Read from the socket until we get EWOULDBLOCK or equivalent.
                    # SSL sockets do some internal buffering, and if the data is
                    # sitting in the SSL object's buffer select() and friends
                    # can't see it; the only way to find out if it's there is to
                    # try to read it.
                    if self._read_to_buffer() == 0:
                        break
            finally:
                self._pending_callbacks -= 1
        except Exception:
            gen_log.warning("error on read", exc_info=True)
            self.close(exc_info=True)
            return
        if self._read_from_buffer():
            return
        else:
            self._maybe_run_close_callback()

    def _set_read_callback(self, callback):
        assert not self._read_callback, "Already reading"
        # 回调函数，即：HTTPConnection的 _on_headers 方法
        self._read_callback = stack_context.wrap(callback)

    def _try_inline_read(self):
        """Attempt to complete the current read operation from buffered data.

        If the read can be completed without blocking, schedules the
        read callback on the next IOLoop iteration; otherwise starts
        listening for reads on the socket.
        """

        # _try_inline_read首先尝试_read_from_buffer，即从上一次的【读缓冲区】中取数据，
        # 如果有数据直接调用 self._run_callback(callback, self._consume(data_length))
        # 执行回调函数，_consume消耗掉了_read_buffer中的数据；
        # 否则即_read_buffer之前没有未读数据，
        # 先通过_read_to_buffer将数据从socket读入_read_buffer，然后再执行_read_from_buffer操作。


        # 看了半天，终于可以看到iostream对外提供的接口了。以read_bytes为例，它首先设置了回调及读取内容的大小，
        # 接着调用_try_inline_read做结。而的_try_inline_read代码如下：

        # 首先尝试从自身缓冲区读取，如果失败则反复调用直到close或者缓冲区里没有东西可读
        # （由于设置了fd为非阻塞模式，read不会被阻塞而是返回0），在此尝试从自身缓冲区读取，
        # 还是没达到要求的话就调用_maybe_add_error_listener，其实就是开始监听read事件。
        # See if we've already got the data from a previous read
        #
        # 先从socket中读取信息并保存到buffer中
        #然后再读取buffer中的数据，以其为参数执行回调函数（HTTPConnection的 _on_headers 方法）
        #buffer其实是一个线程安装的双端队列collections.deque

        #从buffer中读取数据，并执行回调函数。
        #注意：首次执行时buffer中没有数据
        if self._read_from_buffer():
            return
        self._check_closed()
        try:
            try:
                # See comments in _handle_read about incrementing _pending_callbacks
                # 还有个 _pending_callbacks 信号量，作用稍候再说。
                # IOStream相对就比较简单了，主要是实现了socket的读写，叫它SocketStream也许更合适？
                # 最后是关于_pending_callbacks 信号量。
                # 首先它总是成对出现，有增就有减，并且总是【先增后减】。
                # 另外，凡是在它减一后总是会执行 _maybe_run_close_callback
                # 或者 _maybe_add_error_listener。是的，没错，
                # _pending_callbacks只对这两个函数起作用。
                # 根据注释的说法，信号量的作用：是为了防止读缓冲区中部出现的‘’空字符串导致被误认为close，
                # 为了防止所有的回调都不会因为空字符串而被close所中断，就使用信号量告诉系统现在暂时不要close。
                # 而且由于信号量增减后总是会调用两个函数之一，因此close回调总是会被调用
                # 而不会因为信号量而没有得到正确执行）。
                self._pending_callbacks += 1
                while not self.closed():
                    # 从socket中读取信息到buffer（线程安全的一个双向消息队列）
                    if self._read_to_buffer() == 0:
                        break
            finally:
                self._pending_callbacks -= 1
        except Exception:
            # If there was an in _read_to_buffer, we called close() already,
            # but couldn't run the close callback because of _pending_callbacks.
            # Before we escape from this function, run the close callback if
            # applicable.
            self._maybe_run_close_callback()
            raise
        if self._read_from_buffer():
            return
        self._maybe_add_error_listener()

    def _read_to_buffer(self):
        """Reads from the socket and appends the result to the read buffer.

        Returns the number of bytes read.  Returns 0 if there is nothing
        to read (i.e. the read returns EWOULDBLOCK or equivalent).  On
        error closes the socket and raises an exception.
        """
        try:
            # 首先是调用read_from_fd函数(由子类覆盖重写，简单的认为就是fd.read()）得到chunk。
            ## def self.read_from_fd()
					# chunk = self.socket.recv(self.read_chunk_size)
            chunk = self.read_from_fd()
            # 一般fd可读时操作系统缓冲区里都会有一定长度的chunk，所以一般总是能得到某个chunk
            # （但不一定是符合预期的chunk，比如我希望将所有的内容读完直到结束，
            # 但系统缓冲区里不一定就放的下。。。）。
            # 得到chunk后，把它放到自己的缓冲区里（这样操作系统的缓冲区就可以复用为新内容服务）
            # 并增加buffesize，检查是否超过了缓冲区最大允许容量，最后返回chunk的大小
        except (socket.error, IOError, OSError) as e:
            # ssl.SSLError is a subclass of socket.error
            if e.args[0] == errno.ECONNRESET:
                # Treat ECONNRESET as a connection close rather than
                # an error to minimize log spam  (the exception will
                # be available on self.error for apps that care).
                self.close(exc_info=True)
                return
            self.close(exc_info=True)
            raise
        if chunk is None:
            return 0
        self._read_buffer.append(chunk)
        self._read_buffer_size += len(chunk)
        if self._read_buffer_size >= self.max_buffer_size:
            gen_log.error("Reached maximum read buffer size")
            self.close()
            raise IOError("Reached maximum read buffer size")
        return len(chunk)

    def _read_from_buffer(self):
        """Attempts to complete the currently-pending read from the buffer.

        Returns True if the read was completed.
        """
        # 试图从缓冲区中完成当前挂起的读取。

        # _try_inline_read首先尝试_read_from_buffer，即从上一次的读缓冲区中取数据，
        # 如果有数据直接调用 self._run_callback(callback, self._consume(data_length))
        # 执行回调函数，_consume消耗掉了_read_buffer中的数据；

        # 然后是read_from_buffer，这个函数比较重要了，
        # 因为iostream需要支持很多种读的方式，例如rea_until,read_bytes,read_regex等，
        # 这些模式和对应的callback都是在这个函数里被实现和调用的：

        # 首先是检查_streaming_callback回调
        # （字符流回调，一般是读操作没有彻底读够而处于streaming状态，一般默认是None，
        # 如果调用read_bytes和read_until_close并指定了streaming_callback参数就会造成这个回调）和buffersize。
        # 如果read_bytes没有被设定则说明调用的是read_until_close，则直接把buffer里所有内容读出并调用回调，
        # 否则的话根据read_bytes和buffersize决定要读取的大小，其它步骤同read_until_close。这只是开胃菜，
        # 然后开始判断【各个标志】来决定【回调】。不同的标志在各自读函数里分别设置，在此就不一一赘述了。
        # 简言之就是根据不同的标志采取不同的条件判断，如果判断成功就回调。
        if self._streaming_callback is not None and self._read_buffer_size:
            bytes_to_consume = self._read_buffer_size
            # 构造函数中默认设置为None
            if self._read_bytes is not None:
                bytes_to_consume = min(self._read_bytes, bytes_to_consume)
                self._read_bytes -= bytes_to_consume

            # 消耗掉了_read_buffer中的数据；
            self._run_callback(self._streaming_callback,
                               self._consume(bytes_to_consume))
        if self._read_bytes is not None and self._read_buffer_size >= self._read_bytes:
            num_bytes = self._read_bytes
            callback = self._read_callback
            self._read_callback = None
            self._streaming_callback = None
            self._read_bytes = None
            self._run_callback(callback, self._consume(num_bytes))
            return True

        # _read_delimiter的值为 \r\n\r\n
        elif self._read_delimiter is not None:
            # Multi-byte delimiters (e.g. '\r\n') may straddle two
            # chunks in the read buffer, so we can't easily find them
            # without collapsing the buffer.  However, since protocols
            # using delimited reads (as opposed to reads of a known
            # length) tend to be "line" oriented, the delimiter is likely
            # to be in the first few chunks.  Merge the buffer gradually
            # since large merges are relatively expensive and get undone in
            # consume().
            if self._read_buffer:
                while True:
                    loc = self._read_buffer[0].find(self._read_delimiter)
                    if loc != -1:
                        callback = self._read_callback
                        delimiter_len = len(self._read_delimiter)
                        self._read_callback = None
                        self._streaming_callback = None
                        self._read_delimiter = None
                        self._run_callback(callback,
                                           self._consume(loc + delimiter_len))
                        return True
                    if len(self._read_buffer) == 1:
                        break
                    _double_prefix(self._read_buffer)
        elif self._read_regex is not None:
            if self._read_buffer:
                while True:
                    m = self._read_regex.search(self._read_buffer[0])
                    if m is not None:
                        callback = self._read_callback
                        self._read_callback = None
                        self._streaming_callback = None
                        self._read_regex = None
                        self._run_callback(callback, self._consume(m.end()))
                        return True
                    if len(self._read_buffer) == 1:
                        break
                    _double_prefix(self._read_buffer)
        return False

    def _handle_write(self):

        # 调用socket.send给客户端发送响应数据
        # 执行回调函数HTTPConnection的_on_write_complete方法
        while self._write_buffer:
            try:
                if not self._write_buffer_frozen:
                    # On windows, socket.send blows up if given a
                    # write buffer that's too large, instead of just
                    # returning the number of bytes it was able to
                    # process.  Therefore we must not call socket.send
                    # with more than 128KB at a time.

                    #它的作用是将【双端队列（deque）】的[首项]调整为指定大小，
                    _merge_prefix(self._write_buffer, 128 * 1024)
                # #调用客户端socket对象的send方法发送数据
                # self.write_to_fd( ) 函数返回内容：return self.socket.send(data)
                num_bytes = self.write_to_fd(self._write_buffer[0])
                if num_bytes == 0:
                    # With OpenSSL, if we couldn't write the entire buffer,
                    # the very same string object must be used on the
                    # next call to send.  Therefore we suppress
                    # merging the write buffer after an incomplete send.
                    # A cleaner solution would be to set
                    # SSL_MODE_ACCEPT_MOVING_WRITE_BUFFER, but this is
                    # not yet accessible from python
                    # (http://bugs.python.org/issue8240)
                    self._write_buffer_frozen = True
                    break

                # 写缓冲区的冻结（是否要冻结）
                self._write_buffer_frozen = False
                _merge_prefix(self._write_buffer, num_bytes)
                self._write_buffer.popleft()
            except socket.error as e:
                if e.args[0] in (errno.EWOULDBLOCK, errno.EAGAIN):
                    self._write_buffer_frozen = True
                    break
                else:
                    if e.args[0] not in (errno.EPIPE, errno.ECONNRESET):
                        # Broken pipe errors are usually caused by connection
                        # reset, and its better to not log EPIPE errors to
                        # minimize log spam
                        gen_log.warning("Write error on %d: %s",
                                        self.fileno(), e)
                    self.close(exc_info=True)
                    return
        if not self._write_buffer and self._write_callback:
            callback = self._write_callback
            self._write_callback = None
            # 执行回调函数关闭客户端socket连接（HTTPConnection的_on_write_complete方法
            #  self._run_callback就是一个执行回调函数的函数
            self._run_callback(callback)

    def _consume(self, loc):

        # 合并读缓冲区loc个字节，从读缓冲区删除并返回这些数据

        #  _consume，它用作从自身缓冲区中取出指定长度的内容。
        # 代码就不贴了，流程很简单，
        # 先_merge_prefix使缓冲区首项符合指定大小，
        # 再popleft【弹出首项】并调整buffersize即可
        if loc == 0:
            return b""
        _merge_prefix(self._read_buffer, loc)
        # 取出数据之后缓冲区的数据将减少 loc个字节
        self._read_buffer_size -= loc
        return self._read_buffer.popleft()

    def _check_closed(self):
        if self.closed():
            raise StreamClosedError("Stream is closed")

    def _maybe_add_error_listener(self):
        if self._state is None and self._pending_callbacks == 0:
            if self.closed():
                self._maybe_run_close_callback()
            else:
                self._add_io_state(ioloop.IOLoop.READ)


    # 和ioloop异步有关的是两个函数：_add_io_state 和 _handle_events
    def _add_io_state(self, state):
        """Adds `state` (IOLoop.{READ,WRITE} flags) to our event handler.

        Implementation notes: Reads and writes have a fast path and a
        slow path.  The fast path reads synchronously from socket
        buffers, while the slow path uses `_add_io_state` to schedule
        an IOLoop callback.  Note that in both cases, the callback is
        run asynchronously with `_run_callback`.

        To detect closed connections, we must have called
        `_add_io_state` at some point, but we want to delay this as
        much as possible so we don't have to set an `IOLoop.ERROR`
        listener that will be overwritten by the next slow-path
        operation.  As long as there are callbacks scheduled for
        fast-path ops, those callbacks may do more reads.
        If a sequence of fast-path ops do not end in a slow-path op,
        (e.g. for an @asynchronous long-poll request), we must add
        the error handler.  This is done in `_run_callback` and `write`
        (since the write callback is optional so we can have a
        fast-path write with no `_run_callback`)
        """

        # 为IOLoop对象的handler注册IOLoop.READ或IOLoop.WRITE状态，
        # handler为IOStream对象的_handle_events方法。


        # _add_io_state，主要是【更新】自身的状态并告诉ioloop监听新事件。
        if self.closed():
            # connection has been closed, so there can be no future events
            return
        if self._state is None:
            self._state = ioloop.IOLoop.ERROR | state
            # #将客户端socket句柄添加的epoll中，并将IOStream的_handle_events方法添加到 Start 的While循环中
            #Start 的While循环中监听客户端socket句柄的状态，以便再最后调用IOStream的_handle_events方法把处理后的信息响应给用户
            with stack_context.NullContext():
                self.io_loop.add_handler(
                    self.fileno(), self._handle_events, self._state)
        elif not self._state & state:
            self._state = self._state | state
            self.io_loop.update_handler(self.fileno(), self._state)


class IOStream(BaseIOStream):
    r"""Socket-based `IOStream` implementation.

    This class supports the read and write methods from `BaseIOStream`
    plus a `connect` method.

    The ``socket`` parameter may either be connected or unconnected.
    For server operations the socket is the result of calling
    `socket.accept <socket.socket.accept>`.  For client operations the
    socket is created with `socket.socket`, and may either be
    connected before passing it to the `IOStream` or connected with
    `IOStream.connect`.

    A very simple (and broken) HTTP client using this class::

        import tornado.ioloop
        import tornado.iostream
        import socket

        def send_request():
            stream.write(b"GET / HTTP/1.0\r\nHost: friendfeed.com\r\n\r\n")
            stream.read_until(b"\r\n\r\n", on_headers)

        def on_headers(data):
            headers = {}
            for line in data.split(b"\r\n"):
               parts = line.split(b":")
               if len(parts) == 2:
                   headers[parts[0].strip()] = parts[1].strip()
            stream.read_bytes(int(headers[b"Content-Length"]), on_body)

        def on_body(data):
            print data
            stream.close()
            tornado.ioloop.IOLoop.instance().stop()

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        stream = tornado.iostream.IOStream(s)
        stream.connect(("friendfeed.com", 80), send_request)
        tornado.ioloop.IOLoop.instance().start()
    """
    def __init__(self, socket, *args, **kwargs):

        # IOStream对socket读写进行了封装，分别提供读、写缓冲区实现对socket的异步读写。
        # 当socket被accept之后HTTPServer的_handle_connection会被回调并初始化IOStream对象，
        # 进一步通过IOStream提供的功能接口完成socket的读写。文章接下来将关注IOStream实现【读写的细节】

        # IOStream的初始化
        # IOStream初始化过程中主要完成以下操作：

        # 1.绑定对应的socket
        # 2.绑定ioloop
        # 3.创建【读】缓冲区_read_buffer，一个python 【deque容器】
        # 4.创建【写】缓冲区_write_buffer，同样也是一个python 【deque容器】
        # 5.IOStream提供的主要功能接口

        # 主要的[读写]接口包括以下四个：

        # class IOStream(object):
        # 	def read_until(self, delimiter, callback):
        # 	def read_bytes(self, num_bytes, callback, streaming_callback=None):

        # read_until和read_bytes是最常用的[读]接口，
        # 它们工作的过程【都】是：
        # 1.先注册读事件结束时调用的回调函数，
        # 2.然后调用_try_inline_read方法

        # read_until和read_bytes的区别在于：
        # _read_from_buffer过程中截取数据的方法不同，
        # read_until读取到delimiter终止，而read_bytes则读取num_bytes个字节终止。


        # read_until_regex相当于delimiter为某一正则表达式的read_until。
        # def read_until_regex(self, regex, callback):

        # read_until_close主要用于IOStream流关闭前后的读取
        # def read_until_close(self, callback, streaming_callback=None):

        # write首先将data按照【数据块大小】WRITE_BUFFER_CHUNK_SIZE【分块】写入write_buffer，
        # 然后调用handle_write向socket发送数据。
        # def write(self, data, callback=None):

        #


        self.socket = socket
        self.socket.setblocking(False)
        super(IOStream, self).__init__(*args, **kwargs)

    def fileno(self):
        return self.socket.fileno()

    def close_fd(self):
        self.socket.close()
        self.socket = None

    def get_fd_error(self):
        errno = self.socket.getsockopt(socket.SOL_SOCKET,
                                       socket.SO_ERROR)
        return socket.error(errno, os.strerror(errno))

    def read_from_fd(self):
        try:
            chunk = self.socket.recv(self.read_chunk_size)
        except socket.error as e:
            if e.args[0] in (errno.EWOULDBLOCK, errno.EAGAIN):
                return None
            else:
                raise
        if not chunk:
            self.close()
            return None
        return chunk

    def write_to_fd(self, data):
        return self.socket.send(data)

    def connect(self, address, callback=None, server_hostname=None):
        """Connects the socket to a remote address without blocking.

        May only be called if the socket passed to the constructor was
        not previously connected.  The address parameter is in the
        same format as for `socket.connect <socket.socket.connect>`,
        i.e. a ``(host, port)`` tuple.  If ``callback`` is specified,
        it will be called when the connection is completed.

        If specified, the ``server_hostname`` parameter will be used
        in SSL connections for certificate validation (if requested in
        the ``ssl_options``) and SNI (if supported; requires
        Python 3.2+).

        Note that it is safe to call `IOStream.write
        <BaseIOStream.write>` while the connection is pending, in
        which case the data will be written as soon as the connection
        is ready.  Calling `IOStream` read methods before the socket is
        connected works on some platforms but is non-portable.
        """
        self._connecting = True
        try:
            self.socket.connect(address)
        except socket.error as e:
            # In non-blocking mode we expect connect() to raise an
            # exception with EINPROGRESS or EWOULDBLOCK.
            #
            # On freebsd, other errors such as ECONNREFUSED may be
            # returned immediately when attempting to connect to
            # localhost, so handle them the same way as an error
            # reported later in _handle_connect.
            if e.args[0] not in (errno.EINPROGRESS, errno.EWOULDBLOCK):
                gen_log.warning("Connect error on fd %d: %s",
                                self.socket.fileno(), e)
                self.close(exc_info=True)
                return
        self._connect_callback = stack_context.wrap(callback)
        self._add_io_state(self.io_loop.WRITE)

    def _handle_connect(self):
        err = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if err != 0:
            self.error = socket.error(err, os.strerror(err))
            # IOLoop implementations may vary: some of them return
            # an error state before the socket becomes writable, so
            # in that case a connection failure would be handled by the
            # error path in _handle_events instead of here.
            gen_log.warning("Connect error on fd %d: %s",
                            self.socket.fileno(), errno.errorcode[err])
            self.close()
            return
        if self._connect_callback is not None:
            callback = self._connect_callback
            self._connect_callback = None
            self._run_callback(callback)
        self._connecting = False

    def set_nodelay(self, value):
        if (self.socket is not None and
                self.socket.family in (socket.AF_INET, socket.AF_INET6)):
            try:
                self.socket.setsockopt(socket.IPPROTO_TCP,
                                       socket.TCP_NODELAY, 1 if value else 0)
            except socket.error as e:
                # Sometimes setsockopt will fail if the socket is closed
                # at the wrong time.  This can happen with HTTPServer
                # resetting the value to false between requests.
                if e.errno != errno.EINVAL:
                    raise


class SSLIOStream(IOStream):
    """A utility class to write to and read from a non-blocking SSL socket.

    If the socket passed to the constructor is already connected,
    it should be wrapped with::

        ssl.wrap_socket(sock, do_handshake_on_connect=False, **kwargs)

    before constructing the `SSLIOStream`.  Unconnected sockets will be
    wrapped when `IOStream.connect` is finished.
    """
    def __init__(self, *args, **kwargs):
        """The ``ssl_options`` keyword argument may either be a dictionary
        of keywords arguments for `ssl.wrap_socket`, or an `ssl.SSLContext`
        object.
        """
        self._ssl_options = kwargs.pop('ssl_options', {})
        super(SSLIOStream, self).__init__(*args, **kwargs)
        self._ssl_accepting = True
        self._handshake_reading = False
        self._handshake_writing = False
        self._ssl_connect_callback = None
        self._server_hostname = None

    def reading(self):
        return self._handshake_reading or super(SSLIOStream, self).reading()

    def writing(self):
        return self._handshake_writing or super(SSLIOStream, self).writing()

    def _do_ssl_handshake(self):
        # Based on code from test_ssl.py in the python stdlib
        try:
            self._handshake_reading = False
            self._handshake_writing = False
            self.socket.do_handshake()
        except ssl.SSLError as err:
            if err.args[0] == ssl.SSL_ERROR_WANT_READ:
                self._handshake_reading = True
                return
            elif err.args[0] == ssl.SSL_ERROR_WANT_WRITE:
                self._handshake_writing = True
                return
            elif err.args[0] in (ssl.SSL_ERROR_EOF,
                                 ssl.SSL_ERROR_ZERO_RETURN):
                return self.close(exc_info=True)
            elif err.args[0] == ssl.SSL_ERROR_SSL:
                try:
                    peer = self.socket.getpeername()
                except Exception:
                    peer = '(not connected)'
                gen_log.warning("SSL Error on %d %s: %s",
                                self.socket.fileno(), peer, err)
                return self.close(exc_info=True)
            raise
        except socket.error as err:
            if err.args[0] in (errno.ECONNABORTED, errno.ECONNRESET):
                return self.close(exc_info=True)
        except AttributeError:
            # On Linux, if the connection was reset before the call to
            # wrap_socket, do_handshake will fail with an
            # AttributeError.
            return self.close(exc_info=True)
        else:
            self._ssl_accepting = False
            if not self._verify_cert(self.socket.getpeercert()):
                self.close()
                return
            if self._ssl_connect_callback is not None:
                callback = self._ssl_connect_callback
                self._ssl_connect_callback = None
                self._run_callback(callback)

    def _verify_cert(self, peercert):
        """Returns True if peercert is valid according to the configured
        validation mode and hostname.

        The ssl handshake already tested the certificate for a valid
        CA signature; the only thing that remains is to check
        the hostname.
        """
        if isinstance(self._ssl_options, dict):
            verify_mode = self._ssl_options.get('cert_reqs', ssl.CERT_NONE)
        elif isinstance(self._ssl_options, ssl.SSLContext):
            verify_mode = self._ssl_options.verify_mode
        assert verify_mode in (ssl.CERT_NONE, ssl.CERT_REQUIRED, ssl.CERT_OPTIONAL)
        if verify_mode == ssl.CERT_NONE or self._server_hostname is None:
            return True
        cert = self.socket.getpeercert()
        if cert is None and verify_mode == ssl.CERT_REQUIRED:
            gen_log.warning("No SSL certificate given")
            return False
        try:
            ssl_match_hostname(peercert, self._server_hostname)
        except SSLCertificateError:
            gen_log.warning("Invalid SSL certificate", exc_info=True)
            return False
        else:
            return True

    def _handle_read(self):
        if self._ssl_accepting:
            self._do_ssl_handshake()
            return
        super(SSLIOStream, self)._handle_read()

    def _handle_write(self):
        if self._ssl_accepting:
            self._do_ssl_handshake()
            return
        super(SSLIOStream, self)._handle_write()

    def connect(self, address, callback=None, server_hostname=None):
        # Save the user's callback and run it after the ssl handshake
        # has completed.
        self._ssl_connect_callback = stack_context.wrap(callback)
        self._server_hostname = server_hostname
        super(SSLIOStream, self).connect(address, callback=None)

    def _handle_connect(self):
        # When the connection is complete, wrap the socket for SSL
        # traffic.  Note that we do this by overriding _handle_connect
        # instead of by passing a callback to super().connect because
        # user callbacks are enqueued asynchronously on the IOLoop,
        # but since _handle_events calls _handle_connect immediately
        # followed by _handle_write we need this to be synchronous.
        self.socket = ssl_wrap_socket(self.socket, self._ssl_options,
                                      server_hostname=self._server_hostname,
                                      do_handshake_on_connect=False)
        super(SSLIOStream, self)._handle_connect()

    def read_from_fd(self):
        if self._ssl_accepting:
            # If the handshake hasn't finished yet, there can't be anything
            # to read (attempting to read may or may not raise an exception
            # depending on the SSL version)
            return None
        try:
            # SSLSocket objects have both a read() and recv() method,
            # while regular sockets only have recv().
            # The recv() method blocks (at least in python 2.6) if it is
            # called when there is nothing to read, so we have to use
            # read() instead.
            chunk = self.socket.read(self.read_chunk_size)
        except ssl.SSLError as e:
            # SSLError is a subclass of socket.error, so this except
            # block must come first.
            if e.args[0] == ssl.SSL_ERROR_WANT_READ:
                return None
            else:
                raise
        except socket.error as e:
            if e.args[0] in (errno.EWOULDBLOCK, errno.EAGAIN):
                return None
            else:
                raise
        if not chunk:
            self.close()
            return None
        return chunk


class PipeIOStream(BaseIOStream):
    """Pipe-based `IOStream` implementation.

    The constructor takes an integer file descriptor (such as one returned
    by `os.pipe`) rather than an open file object.  Pipes are generally
    one-way, so a `PipeIOStream` can be used for reading or writing but not
    both.
    """
    def __init__(self, fd, *args, **kwargs):
        self.fd = fd
        _set_nonblocking(fd)
        super(PipeIOStream, self).__init__(*args, **kwargs)

    def fileno(self):
        return self.fd

    def close_fd(self):
        os.close(self.fd)

    def write_to_fd(self, data):
        return os.write(self.fd, data)

    def read_from_fd(self):
        try:
            chunk = os.read(self.fd, self.read_chunk_size)
        except (IOError, OSError) as e:
            if e.args[0] in (errno.EWOULDBLOCK, errno.EAGAIN):
                return None
            elif e.args[0] == errno.EBADF:
                # If the writing half of a pipe is closed, select will
                # report it as readable but reads will fail with EBADF.
                self.close(exc_info=True)
                return None
            else:
                raise
        if not chunk:
            self.close()
            return None
        return chunk


def _double_prefix(deque):
    """Grow by doubling, but don't split the second chunk just because the
    first one is small.
    """
    new_len = max(len(deque[0]) * 2,
                  (len(deque[0]) + len(deque[1])))
    _merge_prefix(deque, new_len)


def _merge_prefix(deque, size):
    """Replace the first entries in a deque of strings with a single
    string of up to size bytes.
    ensure the first elements equal condition (count_num or bytes)
    >>> d = collections.deque(['abc', 'de', 'fghi', 'j'])
    >>> _merge_prefix(d, 5); print(d)
    deque(['abcde', 'fghi', 'j'])

    Strings will be split as necessary to reach the desired size.
    >>> _merge_prefix(d, 7); print(d)
    deque(['abcdefg', 'hi', 'j'])

    >>> _merge_prefix(d, 3); print(d)
    deque(['abc', 'defg', 'hi', 'j'])

    >>> _merge_prefix(d, 100); print(d)
    deque(['abcdefghij'])
    """

    # 先认识一个工具函数：_merge_prefix。它的作用是将【双端队列（deque）】的[首项]调整为指定大小，
    # 如果明白双端队列的popleft和appendleft方法，这个函数还是很容易看懂的，我略过对它的分析。
    if len(deque) == 1 and len(deque[0]) <= size:
        return
    prefix = []
    remaining = size
    while deque and remaining > 0:
        chunk = deque.popleft()
        if len(chunk) > remaining:
            deque.appendleft(chunk[remaining:])
            chunk = chunk[:remaining]
        prefix.append(chunk)
        remaining -= len(chunk)
    # This data structure normally just contains byte strings, but
    # the unittest gets messy if it doesn't use the default str() type,
    # so do the merge based on the type of data that's actually present.
    if prefix:
        deque.appendleft(type(prefix[0])().join(prefix))
    if not deque:
        deque.appendleft(b"")


def doctests():
    import doctest
    return doctest.DocTestSuite()

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

"""An I/O event loop for non-blocking sockets.

Typical applications will use a single `IOLoop` object, in the
`IOLoop.instance` singleton.  The `IOLoop.start` method should usually
be called at the end of the ``main()`` function.  Atypical applications may
use more than one `IOLoop`, such as one `IOLoop` per thread, or per `unittest`
case.

In addition to I/O events, the `IOLoop` can also schedule time-based events.
`IOLoop.add_timeout` is a non-blocking alternative to `time.sleep`.
"""

from __future__ import absolute_import, division, print_function, with_statement

import datetime
import errno
import functools
import heapq
import logging
import numbers
import os
import select
import sys
import threading
import time
import traceback

from tornado.concurrent import Future, TracebackFuture
from tornado.log import app_log, gen_log
from tornado import stack_context
from tornado.util import Configurable

try:
    import signal
except ImportError:
    signal = None

try:
    import thread  # py2
except ImportError:
    import _thread as thread  # py3

from tornado.platform.auto import set_close_exec, Waker


class TimeoutError(Exception):
    pass


# 关于ioloop.py的代码，主要有两个要点。
# 一个是configurable机制，
# 一个就是epoll循环。
# 先看epoll循环吧。IOLoop 类的start是循环所在，但它必须【被子类覆盖实现】，因此它的start在PollIOLoop里

class IOLoop(Configurable):
    """A level-triggered I/O loop.

    We use ``epoll`` (Linux) or ``kqueue`` (BSD and Mac OS X) if they
    are available, or else we fall back on select(). If you are
    implementing a system that needs to handle thousands of
    simultaneous connections, you should use a system that supports
    either ``epoll`` or ``kqueue``.

    Example usage for a simple TCP server::

        import errno
        import functools
        import ioloop
        import socket

        def connection_ready(sock, fd, events):
            while True:
                try:
                    connection, address = sock.accept()
                except socket.error, e:
                    if e.args[0] not in (errno.EWOULDBLOCK, errno.EAGAIN):
                        raise
                    return
                connection.setblocking(0)
                handle_connection(connection, address)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setblocking(0)
        sock.bind(("", port))
        sock.listen(128)

        io_loop = ioloop.IOLoop.instance()
        callback = functools.partial(connection_ready, sock)
        io_loop.add_handler(sock.fileno(), callback, io_loop.READ)
        io_loop.start()

    """
    # epoll是Linux内核中实现的一种可扩展的I/O事件【通知】机制，是对POISX系统中 select 和 poll 的替代，
    # 具有更高的性能和扩展性，FreeBSD中类似的实现是kqueue。
    # Tornado中基于Python C扩展实现的的epoll模块(或kqueue)对epoll(kqueue)的使用进行了封装，
    # 使得IOLoop对象可以通过相应的事件处理机制对I/O进行调度。
    # 具体可以参考前面小节的 预备知识：我读过的对epoll最好的讲解 。

    # Constants from the epoll module
    _EPOLLIN = 0x001
    _EPOLLPRI = 0x002
    _EPOLLOUT = 0x004
    _EPOLLERR = 0x008
    _EPOLLHUP = 0x010
    _EPOLLRDHUP = 0x2000
    _EPOLLONESHOT = (1 << 30)
    _EPOLLET = (1 << 31)


    # IOLoop模块对网络事件类型的封装与epoll一致，分为READ / WRITE / ERROR三类，具体在源码里呈现如下：
    # Our events map exactly to the epoll events
    NONE = 0
    READ = _EPOLLIN
    WRITE = _EPOLLOUT
    ERROR = _EPOLLERR | _EPOLLHUP

    # Global lock for creating global IOLoop instance
    _instance_lock = threading.Lock()

    _current = threading.local()

    @staticmethod
    def instance():
        """Returns a global `IOLoop` instance.

        Most applications have a single, global `IOLoop` running on the
        main thread.  Use this method to get this instance from
        another thread.  To get the current thread's `IOLoop`, use `current()`.
        """
        #  IOLoop 是基于 epoll 实现的底层网络I/O的核心调度模块，用于处理 socket 相关的
        # 连接、响应、异步读写等网络事件。每个 Tornado 进程都会初始化一个全局唯一的 IOLoop 实例，
        # 在 IOLoop 中通过静态方法 instance() 进行封装，获取 IOLoop 实例直接调用此方法即可。

        # Tornado 服务器启动时会创建监听 socket，并将 socket 的 [file descriptor] 注册到
        # IOLoop 实例中，IOLoop 添加对 socket 的IOLoop.READ 事件监听并传入回调处理函数。
        # 当某个 socket 通过 accept 接受连接请求后调用注册的回调函数进行读写。
        # 接下来主要分析IOLoop 对 epoll 的封装和 I/O 调度具体实现。

        if not hasattr(IOLoop, "_instance"):
            with IOLoop._instance_lock:
                if not hasattr(IOLoop, "_instance"):
                    # New instance after double check
                    IOLoop._instance = IOLoop()
        return IOLoop._instance

    @staticmethod
    def initialized():
        """Returns true if the singleton instance has been created."""
        return hasattr(IOLoop, "_instance")

    def install(self):
        """Installs this `IOLoop` object as the singleton instance.

        This is normally not necessary as `instance()` will create
        an `IOLoop` on demand, but you may want to call `install` to use
        a custom subclass of `IOLoop`.
        """
        assert not IOLoop.initialized()
        IOLoop._instance = self

    @staticmethod
    def current():
        """Returns the current thread's `IOLoop`.

        If an `IOLoop` is currently running or has been marked as current
        by `make_current`, returns that instance.  Otherwise returns
        `IOLoop.instance()`, i.e. the main thread's `IOLoop`.

        A common pattern for classes that depend on ``IOLoops`` is to use
        a default argument to enable programs with multiple ``IOLoops``
        but not require the argument for simpler applications::

            class MyClass(object):
                def __init__(self, io_loop=None):
                    self.io_loop = io_loop or IOLoop.current()

        In general you should use `IOLoop.current` as the default when
        constructing an asynchronous object, and use `IOLoop.instance`
        when you mean to communicate to the main thread from a different
        one.
        """
        current = getattr(IOLoop._current, "instance", None)
        if current is None:
            return IOLoop.instance()
        return current

    def make_current(self):
        """Makes this the `IOLoop` for the current thread.

        An `IOLoop` automatically becomes current for its thread
        when it is started, but it is sometimes useful to call
        `make_current` explictly before starting the `IOLoop`,
        so that code run at startup time can find the right
        instance.
        """
        IOLoop._current.instance = self

    @staticmethod
    def clear_current():
        IOLoop._current.instance = None

    @classmethod
    def configurable_base(cls):
        return IOLoop


    # 在 Tornado3.0+ 中针对不同平台，单独出 poller 相应的实现，
    # EPollIOLoop、KQueueIOLoop、SelectIOLoop 均继承于 PollIOLoop。
    # 下边的代码是 configurable_default 方法根据平台选择相应的 epoll 实现。
    # 初始化 IOLoop 的过程中会自动根据平台选择合适的 poller 的实现方法。

    @classmethod
    def configurable_default(cls):
        if hasattr(select, "epoll"):
            from tornado.platform.epoll import EPollIOLoop
            return EPollIOLoop
        if hasattr(select, "kqueue"):
            # Python 2.6+ on BSD or Mac
            from tornado.platform.kqueue import KQueueIOLoop
            return KQueueIOLoop
        from tornado.platform.select import SelectIOLoop
        return SelectIOLoop

    def initialize(self):
        pass

    def close(self, all_fds=False):
        """Closes the `IOLoop`, freeing any resources used.

        If ``all_fds`` is true, all file descriptors registered on the
        IOLoop will be closed (not just the ones created by the
        `IOLoop` itself).

        Many applications will only use a single `IOLoop` that runs for the
        entire lifetime of the process.  In that case closing the `IOLoop`
        is not necessary since everything will be cleaned up when the
        process exits.  `IOLoop.close` is provided mainly for scenarios
        such as unit tests, which create and destroy a large number of
        ``IOLoops``.

        An `IOLoop` must be completely stopped before it can be closed.  This
        means that `IOLoop.stop()` must be called *and* `IOLoop.start()` must
        be allowed to return before attempting to call `IOLoop.close()`.
        Therefore the call to `close` will usually appear just after
        the call to `start` rather than near the call to `stop`.

        .. versionchanged:: 3.1
           If the `IOLoop` implementation supports non-integer objects
           for "file descriptors", those objects will have their
           ``close`` method when ``all_fds`` is true.
        """
        raise NotImplementedError()

    def add_handler(self, fd, handler, events):
        """Registers the given handler to receive the given events for fd.

        The ``events`` argument is a bitwise or of the constants
        ``IOLoop.READ``, ``IOLoop.WRITE``, and ``IOLoop.ERROR``.

        When an event occurs, ``handler(fd, events)`` will be run.
        """
        raise NotImplementedError()

    def update_handler(self, fd, events):
        """Changes the events we listen for fd."""
        raise NotImplementedError()

    def remove_handler(self, fd):
        """Stop listening for events on fd."""
        raise NotImplementedError()

    def set_blocking_signal_threshold(self, seconds, action):
        """Sends a signal if the `IOLoop` is blocked for more than
        ``s`` seconds.

        Pass ``seconds=None`` to disable.  Requires Python 2.6 on a unixy
        platform.

        The action parameter is a Python signal handler.  Read the
        documentation for the `signal` module for more information.
        If ``action`` is None, the process will be killed if it is
        blocked for too long.
        """
        raise NotImplementedError()

    def set_blocking_log_threshold(self, seconds):
        """Logs a stack trace if the `IOLoop` is blocked for more than
        ``s`` seconds.

        Equivalent to ``set_blocking_signal_threshold(seconds,
        self.log_stack)``
        """
        self.set_blocking_signal_threshold(seconds, self.log_stack)

    def log_stack(self, signal, frame):
        """Signal handler to log the stack trace of the current thread.

        For use with `set_blocking_signal_threshold`.
        """
        gen_log.warning('IOLoop blocked for %f seconds in\n%s',
                        self._blocking_signal_threshold,
                        ''.join(traceback.format_stack(frame)))

    def start(self):
        """Starts the I/O loop.

        The loop will run until one of the callbacks calls `stop()`, which
        will make the loop stop after the current event iteration completes.
        """
        # OLoop 是 tornado 的核心。程序中主函数通常调用 tornado.ioloop.IOLoop.instance().start()
        # 来启动IOLoop，但是看了一下 IOLoop 的实现，start 方法是这样的：
        # 也就是说 IOLoop 是个[抽象的基类]，具体工作是由它的子类负责的。由于是 Linux 平台，所以应该用 Epoll，
        # 对应的类是 PollIOLoop。PollIOLoop 的 start 方法开始了事件循环。

        # 问题来了，tornado.ioloop.IOLoop.instance() 是怎么返回 PollIOLoop 实例的呢？
        # 刚开始有点想不明白，后来看了一下 IOLoop 的代码就豁然开朗了。
        # IOLoop 继承自 Configurable，后者位于 tornado/util.py。
        # A configurable interface is an (abstract) class
        # whose constructor acts as a factory function for one of its
        # implementation subclasses. The implementation subclass as well as optional
        # keyword arguments to its initializer can be set globally at runtime with
        # configure.

        # Configurable 类实现了一个工厂方法，也就是设计模式中的“工厂模式”，看一下__new__函数的实现：
        # def __new__(cls, **kwargs):
        # 	base = cls.configurable_base()
        # 	args = {}
        # 	if cls is base:
        # 		impl = cls.configured_class()
        # 		if base.__impl_kwargs:
        # 			args.update(base.__impl_kwargs)
        # 	else:
        # 		impl = cls
        # 	args.update(kwargs)
        # 	instance = super(Configurable, cls).__new__(impl)
        # 	 initialize vs __init__ chosen for compatiblity with AsyncHTTPClient
        #   singleton magic.  If we get rid of that we can switch to __init__
    	#   here too.
	    #   instance.initialize(**args)
	    #   return instance
        # 当创建一个Configurable类的实例的时候，其实创建的是configurable_class()返回的类的实例。
        # @classmethod
        # def configured_class(cls):
        # 	"""Returns the currently configured class."""
        # 	base = cls.configurable_base()
        # 	if cls.__impl_class is None:
        # 		base.__impl_class = cls.configurable_default()
        # 	return base.__impl_class

        # 最后，就是返回的configurable_default()。此函数在IOLoop中的实现如下：

        # @classmethod
        # def configurable_default(cls):
        #     if hasattr(select, "epoll"):
        #         from tornado.platform.epoll import EPollIOLoop
        #         return EPollIOLoop
        #     if hasattr(select, "kqueue"):
        #         Python 2.6+ on BSD or Mac
        #         from tornado.platform.kqueue import KQueueIOLoop
        #         return KQueueIOLoop
        #     from tornado.platform.select import SelectIOLoop
        #     return SelectIOLoop
        #
        # EPollIOLoop 是 PollIOLoop 的子类。至此，这个流程就理清楚了。




        raise NotImplementedError()

    def stop(self):
        """Stop the I/O loop.

        If the event loop is not currently running, the next call to `start()`
        will return immediately.

        To use asynchronous methods from otherwise-synchronous code (such as
        unit tests), you can start and stop the event loop like this::

          ioloop = IOLoop()
          async_method(ioloop=ioloop, callback=ioloop.stop)
          ioloop.start()

        ``ioloop.start()`` will return after ``async_method`` has run
        its callback, whether that callback was invoked before or
        after ``ioloop.start``.

        Note that even after `stop` has been called, the `IOLoop` is not
        completely stopped until `IOLoop.start` has also returned.
        Some work that was scheduled before the call to `stop` may still
        be run before the `IOLoop` shuts down.
        """
        raise NotImplementedError()

    def run_sync(self, func, timeout=None):
        """Starts the `IOLoop`, runs the given function, and stops the loop.

        If the function returns a `.Future`, the `IOLoop` will run
        until the future is resolved.  If it raises an exception, the
        `IOLoop` will stop and the exception will be re-raised to the
        caller.

        The keyword-only argument ``timeout`` may be used to set
        a maximum duration for the function.  If the timeout expires,
        a `TimeoutError` is raised.

        This method is useful in conjunction with `tornado.gen.coroutine`
        to allow asynchronous calls in a ``main()`` function::

            @gen.coroutine
            def main():
                # do stuff...

            if __name__ == '__main__':
                IOLoop.instance().run_sync(main)
        """
        future_cell = [None]

        def run():
            try:
                result = func()
            except Exception:
                future_cell[0] = TracebackFuture()
                future_cell[0].set_exc_info(sys.exc_info())
            else:
                if isinstance(result, Future):
                    future_cell[0] = result
                else:
                    future_cell[0] = Future()
                    future_cell[0].set_result(result)
            self.add_future(future_cell[0], lambda future: self.stop())
        self.add_callback(run)
        if timeout is not None:
            timeout_handle = self.add_timeout(self.time() + timeout, self.stop)
        self.start()
        if timeout is not None:
            self.remove_timeout(timeout_handle)
        if not future_cell[0].done():
            raise TimeoutError('Operation timed out after %s seconds' % timeout)
        return future_cell[0].result()

    def time(self):
        """Returns the current time according to the `IOLoop`'s clock.

        The return value is a floating-point number relative to an
        unspecified time in the past.

        By default, the `IOLoop`'s time function is `time.time`.  However,
        it may be configured to use e.g. `time.monotonic` instead.
        Calls to `add_timeout` that pass a number instead of a
        `datetime.timedelta` should use this function to compute the
        appropriate time, so they can work no matter what time function
        is chosen.
        """
        return time.time()

    def add_timeout(self, deadline, callback):
        """Runs the ``callback`` at the time ``deadline`` from the I/O loop.

        Returns an opaque handle that may be passed to
        `remove_timeout` to cancel.

        ``deadline`` may be a number denoting a time (on the same
        scale as `IOLoop.time`, normally `time.time`), or a
        `datetime.timedelta` object for a deadline relative to the
        current time.

        Note that it is not safe to call `add_timeout` from other threads.
        Instead, you must use `add_callback` to transfer control to the
        `IOLoop`'s thread, and then call `add_timeout` from there.
        """
        raise NotImplementedError()

    def remove_timeout(self, timeout):
        """Cancels a pending timeout.

        The argument is a handle as returned by `add_timeout`.  It is
        safe to call `remove_timeout` even if the callback has already
        been run.
        """
        raise NotImplementedError()

    def add_callback(self, callback, *args, **kwargs):
        """Calls the given callback on the next I/O loop iteration.

        It is safe to call this method from any thread at any time,
        except from a signal handler.  Note that this is the **only**
        method in `IOLoop` that makes this thread-safety guarantee; all
        other interaction with the `IOLoop` must be done from that
        `IOLoop`'s thread.  `add_callback()` may be used to transfer
        control from other threads to the `IOLoop`'s thread.

        To add a callback from a signal handler, see
        `add_callback_from_signal`.
        """
        raise NotImplementedError()

    def add_callback_from_signal(self, callback, *args, **kwargs):
        """Calls the given callback on the next I/O loop iteration.

        Safe for use from a Python signal handler; should not be used
        otherwise.

        Callbacks added with this method will be run without any
        `.stack_context`, to avoid picking up the context of the function
        that was interrupted by the signal.
        """
        raise NotImplementedError()

    def add_future(self, future, callback):
        """Schedules a callback on the ``IOLoop`` when the given
        `.Future` is finished.

        The callback is invoked with one argument, the
        `.Future`.
        """
        assert isinstance(future, Future)
        callback = stack_context.wrap(callback)
        future.add_done_callback(
            lambda future: self.add_callback(callback, future))

    def _run_callback(self, callback):
        """Runs a callback with error handling.

        For use in subclasses.
        """
        try:
            callback()
        except Exception:
            # 输出错误日志
            self.handle_callback_exception(callback)

    def handle_callback_exception(self, callback):
        """This method is called whenever a callback run by the `IOLoop`
        throws an exception.

        By default simply logs the exception as an error.  Subclasses
        may override this method to customize reporting of exceptions.

        The exception itself is not passed explicitly, but is available
        in `sys.exc_info`.
        """
        app_log.error("Exception in callback %r", callback, exc_info=True)


class PollIOLoop(IOLoop):
    """Base class for IOLoops built around a select-like function.

    For concrete implementations, see `tornado.platform.epoll.EPollIOLoop`
    (Linux), `tornado.platform.kqueue.KQueueIOLoop` (BSD and Mac), or
    `tornado.platform.select.SelectIOLoop` (all platforms).
    """
    def initialize(self, impl, time_func=None):
        super(PollIOLoop, self).initialize()
        self._impl = impl
        if hasattr(self._impl, 'fileno'):
            set_close_exec(self._impl.fileno())
        self.time_func = time_func or time.time
        self._handlers = {}
        self._events = {}
        self._callbacks = []
        self._callback_lock = threading.Lock()
        self._timeouts = []
        self._cancellations = 0
        self._running = False
        self._stopped = False
        self._closing = False
        self._thread_ident = None
        self._blocking_signal_threshold = None

        # Create a pipe that we send bogus data to when we want to wake
        # the I/O loop when it is idle
        # 在 PollIOLoop(IOLoop) 初始化的过程中创建了一个 Waker 对象，
        # 将 Waker 对象 fd 的【读】端注册到事件循环中并设定相应的回调函数
        # （这样做的好处是当事件循环阻塞而没有响应描述符出现，需要在最大 timeout 时间之前返回，
        # 就可以向这个管道发送一个字符）。

        # Waker 的使用：
        # 一种是在其他线程向 IOLoop 添加 callback 时使用，
        # 唤醒 IOLoop 同时会将控制权转移给 IOLoop 线程并完成特定请求。
        # 唤醒的方法向管道中写入一个字符'x'。
        # 另外，在 IOLoop的stop 函数中会调用self._waker.wake()，通过向管道写入'x'停止事件循环

        self._waker = Waker()
        self.add_handler(self._waker.fileno(),
                         lambda fd, events: self._waker.consume(),
                         self.READ)

    def close(self, all_fds=False):
        with self._callback_lock:
            self._closing = True
        self.remove_handler(self._waker.fileno())
        if all_fds:
            for fd in self._handlers.keys():
                try:
                    close_method = getattr(fd, 'close', None)
                    if close_method is not None:
                        close_method()
                    else:
                        os.close(fd)
                except Exception:
                    gen_log.debug("error closing fd %s", fd, exc_info=True)
        self._waker.close()
        self._impl.close()

    def add_handler(self, fd, handler, events):

        #add_handler(self, fd, handler, events)

        # HttpServer的Start方法中会调用该方法(待考证)

        # add_handler 函数使用了stack_context 提供的 wrap 方法。
        # wrap 返回了一个可以直接调用的对象并且保存了传入之前的【堆栈】信息，
        # 在执行时可以恢复，这样就保证了函数的异步调用时具有正确的运行环境。

        #
        self._handlers[fd] = stack_context.wrap(handler)
        # self._impl 就是epoll 对象；向epoll中注册、监听socket对象
        self._impl.register(fd, events | self.ERROR)

    def update_handler(self, fd, events):
        self._impl.modify(fd, events | self.ERROR)

    def remove_handler(self, fd):
        self._handlers.pop(fd, None)
        self._events.pop(fd, None)
        try:
            self._impl.unregister(fd)
        except Exception:
            gen_log.debug("Error deleting fd from IOLoop", exc_info=True)

    def set_blocking_signal_threshold(self, seconds, action):
        if not hasattr(signal, "setitimer"):
            gen_log.error("set_blocking_signal_threshold requires a signal module "
                          "with the setitimer method")
            return
        self._blocking_signal_threshold = seconds
        if seconds is not None:
            signal.signal(signal.SIGALRM,
                          action if action is not None else signal.SIG_DFL)

    def start(self):

        # IOLoop的start方法
        # IOLoop 的【核心调度】集中在 start() 方法中，
        # IOLoop 实例对象调用 start 后开始 【epoll 事件循环机制】，
        # 该方法会一直运行直到 IOLoop 对象调用 stop 函数、当前所有事件循环完成。
        # start 方法中主要分三个部分：
        # 一个部分是对【超时】的相关处理；
        # 一部分是 epoll 事件通知阻塞、接收；
        # 一部分是对 epoll 返回I/O事件的处理;
        #

        # 为防止 IO event starvation（挨饿，哈哈），将回调函数【延迟到下一轮事件循环中执行。
        # 超时的处理 heapq 维护一个最小堆，记录每个回调函数的超时时间（deadline）。
        # 每次取出 deadline 最早的回调函数，如果callback标志位为 True 并且已经超时，
        # 通过 _run_callback 调用函数；如果没有超时需要重新设定 poll_timeout 的值。
        # 通过 self._impl.poll(poll_timeout) 进行事件阻塞，
        # 当有事件通知或超时时 poll 返回【特定】的 event_pairs。
        # epoll 返回通知事件后将新事件加入待处理队列，将就绪事件逐个【弹出】，
        # 通过stack_context.wrap(handler)保存的可执行对象调用事件处理。

        if not logging.getLogger().handlers:
            # The IOLoop catches and logs exceptions, so it's
            # important that log output be visible.  However, python's
            # default behavior for non-root loggers (prior to python
            # 3.2) is to print an unhelpful "no handlers could be
            # found" message rather than the actual log entry, so we
            # must explicitly configure logging(我们必须显式配置日志记录) if we've made it this
            # far without anything.
            logging.basicConfig()
        if self._stopped:
            self._stopped = False
            return
        # 待研究【18-3.6】
        # instance 是实例的意思
        # 判断是不是有上一次的（或者剩余的IOLoop实例）
        old_current = getattr(IOLoop._current, "instance", None)
        IOLoop._current.instance = self
        self._thread_ident = thread.get_ident()
        self._running = True

        # signal.set_wakeup_fd closes a race condition in event loops:
        # a signal may arrive at the beginning of select/poll/etc
        # before it goes into its interruptible sleep, so the signal
        # will be consumed without waking the select.  The solution is
        # for the (C, synchronous) signal handler to write to a pipe,
        # which will then be seen by select.
        #
        # In python's signal handling semantics, this only matters on the
        # main thread (fortunately, set_wakeup_fd only works on the main
        # thread and will raise a ValueError otherwise).
        #
        # If someone has already set a wakeup fd, we don't want to
        # disturb it.  This is an issue for twisted, which does its
        # SIGCHILD processing in response to its own wakeup fd being
        # written to.  As long as the wakeup fd is registered on the IOLoop,
        # the loop will still wake up and everything should work.
        old_wakeup_fd = None
        # 旧的被唤醒的 fd
        if hasattr(signal, 'set_wakeup_fd') and os.name == 'posix':
            # requires python 2.6+, unix.  set_wakeup_fd exists but crashes
            # the python process on windows.
            try:
                old_wakeup_fd = signal.set_wakeup_fd(self._waker.write_fileno())
                if old_wakeup_fd != -1:
                    # Already set, restore previous value.  This is a little racy,
                    # but there's no clean get_wakeup_fd and in real use the
                    # IOLoop is just started once at the beginning.
                    signal.set_wakeup_fd(old_wakeup_fd)
                    old_wakeup_fd = None
            except ValueError:  # non-main thread
                pass


        # 循环体(重点)
        while True:
            # 首先是设定超时时间为3600秒
            poll_timeout = 3600.0

            # Prevent IO event starvation by delaying new callbacks
            # to the next iteration of the event loop.
            # (防止[保护]饥饿的IO事件是通过推迟新的回调事件循环的下一次迭代)

            # 加上一把线程锁
            # 然后在互斥锁下取出上次循环【遗留】下的回调列表（在add_callback添加对象），
            # 把这次列表置空；
            # 然后依次执行列表里的回调。
            with self._callback_lock:
                # self._callbacks 回到列表中数据
                callbacks = self._callbacks
                self._callbacks = []
            for callback in callbacks:
                # 这里的_run_callback就没什么好分析的了；就是执行剩下的回调函数
                self._run_callback(callback)

            # 紧接着是检查上次循环遗留的超时列表，
            # 如果列表里的项目有回调而且过了截止时间，那肯定超时了，就执行对应的超时回调。
            # 然后检查是否又有了事件回调（因为很多回调函数里可能会再添加回调），
            # 如果是，则不在poll循环里等待，如注释所述
            if self._timeouts:
                now = self.time()
                while self._timeouts:
                    # 这种情况是没有回调函数
                    if self._timeouts[0].callback is None:
                        # the timeout was cancelled（取消超时时间）
                        heapq.heappop(self._timeouts)
                        self._cancellations -= 1
                    # 有回调且过了截至时间；此时就要执行【过时的回调函数】
                    elif self._timeouts[0].deadline <= now:
                        timeout = heapq.heappop(self._timeouts)
                        self._run_callback(timeout.callback)
                    else:
                        seconds = self._timeouts[0].deadline - now
                        poll_timeout = min(seconds, poll_timeout)
                        break
                if (self._cancellations > 512
                        and self._cancellations > (len(self._timeouts) >> 1)):
                    # Clean up the timeout queue when it gets large and it's
                    # more than half cancellations.
                    self._cancellations = 0
                    self._timeouts = [x for x in self._timeouts
                                      if x.callback is not None]
                    heapq.heapify(self._timeouts)

            # 检查是否有事件回调
            if self._callbacks:
                # If any callbacks or timeouts called add_callback,
                # we don't want to wait in poll() before we run them.
                poll_timeout = 0.0

            if not self._running:
                break

            if self._blocking_signal_threshold is not None:
                # clear alarm so it doesn't fire while poll is waiting for
                # events.
                signal.setitimer(signal.ITIMER_REAL, 0, 0)

            try:
                # 这句里的【_impl是epoll】，在platform/epoll.py里定义，
                # 总之就是一个【等待函数】，当有事件（超时也算）发生就返回。
                # 然后把【事件集】保存下来，对于每个事件，self._handlers[fd](fd, events)
                # 根据fd找到回调，并把fd和事件做参数回传。
                # 如果fd是监听的fd，那么这个回调handler就是accept_handler函数，
                # 详见上节代码。如果是新fd可读，一般就是_on_headers 或者 _on_requet_body了，
                # 详见前几节。我好像没看到可写时的回调？以上，就是循环的流程了。
                # 可能还是看的糊里糊涂的，因为很多对象怎么来的都不清楚，
                # configurable也还没有看。看完下面的分析，应该就可以了。
                # Configurable类在util.py里被定义

                # epoll中轮询
                event_pairs = self._impl.poll(poll_timeout)
            except Exception as e:
                # Depending on python version and IOLoop implementation(实施),
                # different exception types may be thrown and there are
                # two ways EINTR might be signaled:
                # * e.errno == errno.EINTR
                # * e.args is like (errno.EINTR, 'Interrupted system call')
                if (getattr(e, 'errno', None) == errno.EINTR or
                    (isinstance(getattr(e, 'args', None), tuple) and
                     len(e.args) == 2 and e.args[0] == errno.EINTR)):
                    continue
                else:
                    raise

            if self._blocking_signal_threshold is not None:
                signal.setitimer(signal.ITIMER_REAL,
                                 self._blocking_signal_threshold, 0)

            # Pop one fd at a time from the set of pending fds and run
            # its handler. Since that handler may perform actions on
            # other file descriptors, there may be reentrant calls to
            # this IOLoop that update self._events

            # 如果有读可用信息，则把【该socket对象句柄和Event Code序列】添加到self._events中
            self._events.update(event_pairs)

            # 遍历self._events，【处理每个请求】
            while self._events:
                fd, events = self._events.popitem()
                try:

                    # 以socket为句柄为key，取出self._handlers中的stack_context.wrap(handler)，并执行
                    # stack_context.wrap(handler)包装了HTTPServer类的_handle_events函数的一个函数
                    # 是在上一步中执行【add_handler方法时候】，添加到self._handlers中的数据。

                    # 跳转到了stack_context.wrap(handler)中
                    # self._handlers[fd] = stack_context.wrap(handler)
                    self._handlers[fd](fd, events)

                except (OSError, IOError) as e:
                    if e.args[0] == errno.EPIPE:
                        # Happens when the client closes the connection
                        pass
                    else:
                        app_log.error("Exception in I/O handler for fd %s",
                                      fd, exc_info=True)
                except Exception:
                    app_log.error("Exception in I/O handler for fd %s",
                                  fd, exc_info=True)
        # reset the stopped flag so another start/stop pair can be issued
        self._stopped = False
        if self._blocking_signal_threshold is not None:
            signal.setitimer(signal.ITIMER_REAL, 0, 0)
        IOLoop._current.instance = old_current
        if old_wakeup_fd is not None:
            signal.set_wakeup_fd(old_wakeup_fd)

    def stop(self):
        self._running = False
        self._stopped = True
        self._waker.wake()

    def time(self):
        return self.time_func()

    def add_timeout(self, deadline, callback):
        timeout = _Timeout(deadline, stack_context.wrap(callback), self)
        heapq.heappush(self._timeouts, timeout)
        return timeout

    def remove_timeout(self, timeout):
        # Removing from a heap is complicated, so just leave the defunct
        # timeout object in the queue (see discussion in
        # http://docs.python.org/library/heapq.html).
        # If this turns out to be a problem, we could add a garbage
        # collection pass whenever there are too many dead timeouts.
        timeout.callback = None
        self._cancellations += 1

    def add_callback(self, callback, *args, **kwargs):
        with self._callback_lock:
            if self._closing:
                raise RuntimeError("IOLoop is closing")
            list_empty = not self._callbacks
            self._callbacks.append(functools.partial(
                stack_context.wrap(callback), *args, **kwargs))
        if list_empty and thread.get_ident() != self._thread_ident:
            # If we're in the IOLoop's thread, we know it's not currently
            # polling.  If we're not, and we added the first callback to an
            # empty list, we may need to wake it up (it may wake up on its
            # own, but an occasional extra wake is harmless).  Waking
            # up a polling IOLoop is relatively expensive, so we try to
            # avoid it when we can.
            self._waker.wake()

    def add_callback_from_signal(self, callback, *args, **kwargs):
        with stack_context.NullContext():
            if thread.get_ident() != self._thread_ident:
                # if the signal is handled on another thread, we can add
                # it normally (modulo the NullContext)
                self.add_callback(callback, *args, **kwargs)
            else:
                # If we're on the IOLoop's thread, we cannot use
                # the regular add_callback because it may deadlock on
                # _callback_lock.  Blindly insert into self._callbacks.
                # This is safe because the GIL makes list.append atomic.
                # One subtlety is that if the signal interrupted the
                # _callback_lock block in IOLoop.start, we may modify
                # either the old or new version of self._callbacks,
                # but either way will work.
                self._callbacks.append(functools.partial(
                    stack_context.wrap(callback), *args, **kwargs))


class _Timeout(object):
    """An IOLoop timeout, a UNIX timestamp and a callback"""

    # Reduce memory overhead when there are lots of pending callbacks
    __slots__ = ['deadline', 'callback']

    def __init__(self, deadline, callback, io_loop):
        if isinstance(deadline, numbers.Real):
            self.deadline = deadline
        elif isinstance(deadline, datetime.timedelta):
            self.deadline = io_loop.time() + _Timeout.timedelta_to_seconds(deadline)
        else:
            raise TypeError("Unsupported deadline %r" % deadline)
        self.callback = callback

    @staticmethod
    def timedelta_to_seconds(td):
        """Equivalent to td.total_seconds() (introduced in python 2.7)."""
        return (td.microseconds + (td.seconds + td.days * 24 * 3600) * 10 ** 6) / float(10 ** 6)

    # Comparison methods to sort by deadline, with object id as a tiebreaker
    # to guarantee a consistent ordering.  The heapq module uses __le__
    # in python2.5, and __lt__ in 2.6+ (sort() and most other comparisons
    # use __lt__).
    def __lt__(self, other):
        return ((self.deadline, id(self)) <
                (other.deadline, id(other)))

    def __le__(self, other):
        return ((self.deadline, id(self)) <=
                (other.deadline, id(other)))


class PeriodicCallback(object):
    """Schedules the given callback to be called periodically.

    The callback is called every ``callback_time`` milliseconds.

    `start` must be called after the `PeriodicCallback` is created.
    """
    def __init__(self, callback, callback_time, io_loop=None):
        self.callback = callback
        if callback_time <= 0:
            raise ValueError("Periodic callback must have a positive callback_time")
        self.callback_time = callback_time
        self.io_loop = io_loop or IOLoop.current()
        self._running = False
        self._timeout = None

    def start(self):
        """Starts the timer."""
        self._running = True
        self._next_timeout = self.io_loop.time()
        self._schedule_next()

    def stop(self):
        """Stops the timer."""
        self._running = False
        if self._timeout is not None:
            self.io_loop.remove_timeout(self._timeout)
            self._timeout = None

    def _run(self):
        if not self._running:
            return
        try:
            self.callback()
        except Exception:
            app_log.error("Error in periodic callback", exc_info=True)
        self._schedule_next()

    def _schedule_next(self):
        if self._running:
            current_time = self.io_loop.time()
            while self._next_timeout <= current_time:
                self._next_timeout += self.callback_time / 1000.0
            self._timeout = self.io_loop.add_timeout(self._next_timeout, self._run)

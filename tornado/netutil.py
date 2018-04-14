#!/usr/bin/env python
#
# Copyright 2011 Facebook
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

"""Miscellaneous network utility code."""

from __future__ import absolute_import, division, print_function, with_statement

import errno
import os
import re
import socket
import ssl
import stat

from tornado.concurrent import dummy_executor, run_on_executor
from tornado.ioloop import IOLoop
from tornado.platform.auto import set_close_exec
from tornado.util import Configurable


def bind_sockets(port, address=None, family=socket.AF_UNSPEC, backlog=128, flags=None):
    """Creates listening sockets bound to the given port and address.

    Returns a list of socket objects (multiple sockets are returned if
    the given address maps to multiple IP addresses, which is most common
    for mixed IPv4 and IPv6 use).

    Address may be either an IP address or hostname.  If it's a hostname,
    the server will listen on all IP addresses associated with the
    name.  Address may be an empty string or None to listen on all
    available interfaces.  Family may be set to either `socket.AF_INET`
    or `socket.AF_INET6` to restrict to IPv4 or IPv6 addresses, otherwise
    both will be used if available.

    The ``backlog`` argument has the same meaning as for
    `socket.listen() <socket.socket.listen>`.

    ``flags`` is a bitmask of AI_* flags to `~socket.getaddrinfo`, like
    ``socket.AI_PASSIVE | socket.AI_NUMERICHOST``.
    """
    # bind_sockets 方法在 netutil.py 里被定义，没什么难的，创建监听 socket 后为了[异步]，
    # 设置 socket 为非阻塞（这样由它 accept 派生的socket 也是非阻塞的），
    # 然后绑定并监听之。
    # add_sockets 方法接收 socket 列表，对于列表中的 socket，用 fd 作键记录下来，
    # 并调用add_accept_handler 方法。它也是在 netutil 里定义的，

    # bind_socket完成的工作包括：(1):创建socket，绑定socket到指定的地址和端口，(2)开启侦听。
    # 函数参数说明：
    # 1.port不用说，端口号嘛。
    # 2.address可以是IP地址，如“192.168.1.100”，也可以是hostname，比如“localhost”。
    # 如果是hostname，则可以监听该hostname对应的【所有IP】。
    # 如果address是空字符串（“”）或者None，则会监听主机上的【所有接口】。
    # 3.family是指网络层协议类型。可以选AF_INET和AF_INET6，默认情况下则两者都会被启用。
    # 这个参数就是在BSD Socket创建时的那个sockaddr_in.sin_family参数。
    # 4.backlog就是指【侦听队列的长度】，即BSD listen(n)中的那个n。
    # 5.flags参数是一些位标志，它是用来传递给socket.getaddrinfo()函数的。比如socket.AI_PASSIVE等。
    # 另外要注意，在IPV6和IPV4【混用】的情况下，这个函数的返回值可以是一个socket列表，
    # 因为这时候一个address参数可能对应一个IPv4地址和一个IPv6地址，它们的socket是【不通用的】，会各自独立创建。


    sockets = []
    if address == "":
        address = None
    if not socket.has_ipv6 and family == socket.AF_UNSPEC:
        # Python can be compiled with --disable-ipv6, which causes
        # operations on AF_INET6 sockets to fail, but does not
        # automatically exclude those results from getaddrinfo
        # results.
        # http://bugs.python.org/issue16208
        family = socket.AF_INET
    if flags is None:
        flags = socket.AI_PASSIVE
    # 闹半天，前面解释的参数全都被[socket.getaddrinfo()]这个函数吃下去了
    # socket.getaddrinfo()是python标准库中的函数，
    # 它的作用是将所接收的参数【重组】为一个结构res，
    # res的【类型】将可以直接作为【socket.socket()】的参数。跟BSD Socket中的getaddrinfo差不多嘛。

    # 之所以用了一个循环，正如前面讲到的，因为IPv6和IPv4【混用】的情况下，
    # getaddrinfo会返回【多个地址】的信息。参见python文档中的说明和示例：
    # The function returns a list of 5-tuples with the following structure: (family, type, proto, canonname, sockaddr)
    # >>> socket.getaddrinfo("www.python.org", 80, proto=socket.SOL_TCP)
    # [(2, 1, 6, '', ('82.94.164.162', 80)),
    #  (10, 1, 6, '', ('2001:888:2000:d::a2', 80, 0, 0))]

    for res in set(socket.getaddrinfo(address, port, family, socket.SOCK_STREAM,
                                      0, flags)):
        # 对 getaddrinfo的详细解释
        # getaddrinfo返回服务器的所有【网卡信息】, 每块网卡上都要【创建监听客户端的请求】并【返回】
        # 创建的sockets。
	    # 创建socket过程中绑定【地址和端口】，同时设置了fcntl.FD_CLOEXEC（创建子进程时关闭打开的socket）和
		# socket.SO_REUSEADDR（保证某一socket关闭后立即释放端口，实现端口复用）标志位。
		# sock.listen(backlog=128)默认设定等待被处理的连接最大个数为128。
        # getaddrinfo(host, port [, family, socktype, proto, flags])
        # -> list of (family, socktype, proto, canonname, sockaddr)
        # Resolve host and port into addrinfo struct.
        # 接下来的代码在循环体中，是针对单个地址的。循环体内一开始就如我们猜想，直接拿getaddrinfo的返回值
        # 来创建socket。
        # 先从tuple中拆出5个参数，然后拣需要的来创建socket。
        af, socktype, proto, canonname, sockaddr = res
        try:
            # 创建 socket
            #
            sock = socket.socket(af, socktype, proto)
        except socket.error as e:
            if e.args[0] == errno.EAFNOSUPPORT:
                continue
            raise
        # 此行暂未理解（3.5/21:50）
        # 同时设置了fcntl.FD_CLOEXEC（创建子进程时关闭打开的socket）
        set_close_exec(sock.fileno())
        # 这行是设置进程退出时对sock的操作。lose_on_exec 是一个进程所有文件描述符
        # （文件句柄）的位图标志，每个比特位代表一个打开的文件描述符，
        # 用于确定在调用系统调用execve()时需要关闭的文件句柄（参见include/fcntl.h）。
        # 当一个程序使用fork()函数创建了一个子进程时，通常会在该子进程中调用execve()函数加载执行另一个新程序。
        # 此时子进程将完全被新程序替换掉，并在子进程中开始执行新程序。
        # 若一个文件描述符在close_on_exec中的对应比特位被设置，
        # 那么在执行execve()时该描述符将被关闭，否则该描述符将始终处于打开状态。

        # 当打开一个文件时，默认情况下文件句柄在子进程中也处于打开状态。因此sys_open()中要复位对应比特位。
        if os.name != 'nt':
            # socket.SO_REUSEADDR（保证某一socket关闭后立即释放端口，实现端口复用）标志位。
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # 对非NT的内核，需要额外设置一个SO_REUSEADDR参数。有些系统的设计里，
            # 服务器进程结束后端口也会被内核保持一段时间，若我们迅速的重启服务器，
            # 可能会遇到“端口已经被占用”的情况。
            # 这个标志就是通知内核不要保持了，进程一关，立马放手，便于后来者重用
        if af == socket.AF_INET6:
            # On linux, ipv6 sockets accept ipv4 too by default,
            # but this makes it impossible to bind to both
            # 0.0.0.0 in ipv4 and :: in ipv6.  On other systems,
            # separate sockets *must* be used to listen for both ipv4
            # and ipv6.  For consistency, always disable ipv4 on our
            # ipv6 sockets and use a separate ipv4 socket when needed.
            #
            # Python 2.x on windows doesn't have IPPROTO_IPV6.
            if hasattr(socket, "IPPROTO_IPV6"):
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)

        # 每创建一个socket都添加到前面定义的sockets = []列表中，函数结束后，将列表返回；但为啥不是tcpserver的成员呢？
        # 网上都说nginx和lighthttpd是高性能web服务器，而tornado也是著名的高抗负载应用，它们间有什么相似处呢？
        # 上节提到的ioloop对象是如何循环的呢？往下看
        # 首先关于TCP服务器的开发上节已经提过，很明显那个【三段式】的示例是个效率很低的
        # （因为只有一个连接被端开新连接才能被接受）。要想开发高性能的服务器，就得在这【accept】上下功夫。

        # 首先，新连接的到来一般是经典的【三次握手】，只有当服务器收到一个SYN时才说明有一个新连接（还没建立），
        # 这时监听[fd是可读的]可以调用accept，此前服务器可以干点别的，这就是SELECT/POLL/EPOLL的思路。
        # 而只有三次握手成功后，accept才会返回，此时监听fd是读完成状态，似乎服务器在此之前可以转身去干别的，
        # 等到读完成再调用accept就不会有延迟了，这就是AIO的思路，不过在*nix平台上好像支持不是很广。。。
        # 再有，accept得到的新fd，不一定是可读的（客户端请求还没到达），所以可以等新fd可读时在read()
        # （可能会有一点延迟），也可以用AIO等读完后再read就不会延迟了。同样类似，对于write，close也有类似的事件。
        # 总的思路就是，在我们关心的fd上注册关心的多个事件，事件发生了就【启动回调】，没发生就看点别的。这是单线程的，
        # 多线程的复杂一点，但差不多。nginx和lightttpd以及tornado都是类似的方式，
        # 只不过是多进程和多线程或单线程的区别而已。为简便，我们只分析tornado单线程的情况

        # 设置为非阻塞的
        sock.setblocking(0)
        # 创建完socket后第一步就是bind(绑定端口地址)
        sock.bind(sockaddr)
        # 绑定完就是要监听；默认设定等待被处理的连接最大个数为128。
        # 最大阻塞数量
        sock.listen(backlog)
        # 把监听到的socket对象添加到socket列表中
        sockets.append(sock)
    return sockets

if hasattr(socket, 'AF_UNIX'):
    def bind_unix_socket(file, mode=0o600, backlog=128):
        """Creates a listening unix socket.

        If a socket with the given name already exists, it will be deleted.
        If any other file with that name exists, an exception will be
        raised.

        Returns a socket object (not a list of socket objects like
        `bind_sockets`)
        """
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        set_close_exec(sock.fileno())
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setblocking(0)
        try:
            st = os.stat(file)
        except OSError as err:
            if err.errno != errno.ENOENT:
                raise
        else:
            if stat.S_ISSOCK(st.st_mode):
                os.remove(file)
            else:
                raise ValueError("File %s exists and is not a socket", file)
        sock.bind(file)
        os.chmod(file, mode)
        sock.listen(backlog)
        return sock


def add_accept_handler(sock, callback, io_loop=None):
    """Adds an `.IOLoop` event handler to accept new connections on ``sock``.

    When a connection is accepted, ``callback(connection, address)`` will
    be run (``connection`` is a socket object, and ``address`` is the
    address of the other end of the connection).  Note that this signature
    is different from the ``callback(fd, events)`` signature used for
    `.IOLoop` handlers.
    """

    # 需要注意的一个参数是 callback，现在指向的是 TCPServer 的 【_handle_connection】 方法。
    # add_accept_handler 方法的流程：
    # 第一：首先是确保ioloop对象。
    # 第二：然后调用 add_handler 向 loloop 对象注册在fd上的read事件和回调函数accept_handler。
    # 该回调函数是现成定义的，属于IOLoop层次的回调，每当事件发生时就会调用。
    # 回调内容也就是accept得到新socket和客户端地址，
    # 第三：然后调用callback向【上层传递事件】。

    # 划重点来了：
    # 从上面的分析可知，当read事件发生时，accept_handler被调用，
    # 进而callback=_handle_connection被调用。

    # _handle_connection就比较简单了，跳过那些ssl的处理，简化为两句：
    # stream = IOStream(connection, io_loop=self.io_loop)和
    # self.handle_stream()。
    # 这里IOStream代表了【IO层】，以后再说，反正读写是不愁了。
    # 接着是调用handle_stream。我们可以看到，不论应用层是什么协议
    # （或者自定义协议），当有新连接到来时走的流程是差不多的，都要经历一番上诉的回调，
    # 不同之处就在于这个handle_stream方法。这个方法是由子类自定义覆盖的，
    # 它的HTTP实现已经在上一节看过了。


    if io_loop is None:
        io_loop = IOLoop.current()

    def accept_handler(fd, events):
        while True:
            try:
                connection, address = sock.accept()
            except socket.error as e:
                # EWOULDBLOCK and EAGAIN indicate we have accepted every
                # connection that is available.
                if e.args[0] in (errno.EWOULDBLOCK, errno.EAGAIN):
                    return
                # ECONNABORTED indicates that there was a connection
                # but it was closed while still in the accept queue.
                # (observed on FreeBSD).
                if e.args[0] == errno.ECONNABORTED:
                    continue
                raise

            # _handle_connection在接受【客户端的连接处理结束之后】会被调用，
            # 调用时传入连接和ioloop对象初始化IOStream对象；其作用是：用于对客户端的异步读写；
            # 然后调用handle_stream，传入创建的IOStream对象初始化一个HTTPConnection对象，
            # HTTPConnection封装了IOStream的一些操作，用于处理HTTPRequest并返回。
            callback(connection, address)
    # 当读取的事件发生时，accept_hander被调用，进而callback=_handle_connection被调用。

    #  执行IOLoop的add_handler方法，将socket句柄（sock.fileno() = self.fd）、
    # [self._handle_events方法]没有看到？
    # 而是看到了：accept_handler 只是先完成了accept;其次是完成了callback(connection, address)
    # 即完成了class tcpserver 中的 _handle_connection()函数 -->【IO层】(也就是读写方面的东西)
    # 与1.1.0中的函数 _handle_events 实现的功能是 一致的；
    # 和IOLoop.READ当参数传入
    # socket对象句柄作为key，被封装了的函数 accept_handler 作为value
    io_loop.add_handler(sock.fileno(), accept_handler, IOLoop.READ)

    # 实际上IOLoop的实例就相当于一个线程，有start, run, stop这些函数。
    # IOLoop实例都包含一个叫_current的成员，指向创建它的线程。每个线程在创建IOLoop时，
    # 都被绑定到创建它的线程上。在IOLoop的类定义中，有这么一行：
    # _current = threading.local()
    # 创建好IOLoop后，下面又定义了一个accept_handler。这是一个函数内定义的函数。
    # accept_handler相当于连接监视器的，所以我们把它绑定到listening socket上。
    # 也就是：io_loop.add_handler(sock.fileno(), accept_handler, IOLoop.READ) 上面
    # 它正式对socket句柄调用 accept。当接收到一个connection后，调用callback()。
    # 不难想到，callback就是客户端连上来后对应的【响应函数】。
    #


def is_valid_ip(ip):
    """Returns true if the given string is a well-formed IP address.

    Supports IPv4 and IPv6.
    """
    try:
        res = socket.getaddrinfo(ip, 0, socket.AF_UNSPEC,
                                 socket.SOCK_STREAM,
                                 0, socket.AI_NUMERICHOST)
        return bool(res)
    except socket.gaierror as e:
        if e.args[0] == socket.EAI_NONAME:
            return False
        raise
    return True


class Resolver(Configurable):
    """Configurable asynchronous DNS resolver interface.

    By default, a blocking implementation is used (which simply calls
    `socket.getaddrinfo`).  An alternative implementation can be
    chosen with the `Resolver.configure <.Configurable.configure>`
    class method::

        Resolver.configure('tornado.netutil.ThreadedResolver')

    The implementations of this interface included with Tornado are

    * `tornado.netutil.BlockingResolver`
    * `tornado.netutil.ThreadedResolver`
    * `tornado.netutil.OverrideResolver`
    * `tornado.platform.twisted.TwistedResolver`
    * `tornado.platform.caresresolver.CaresResolver`
    """
    @classmethod
    def configurable_base(cls):
        return Resolver

    @classmethod
    def configurable_default(cls):
        return BlockingResolver

    def resolve(self, host, port, family=socket.AF_UNSPEC, callback=None):
        """Resolves an address.

        The ``host`` argument is a string which may be a hostname or a
        literal IP address.

        Returns a `.Future` whose result is a list of (family,
        address) pairs, where address is a tuple suitable to pass to
        `socket.connect <socket.socket.connect>` (i.e. a ``(host,
        port)`` pair for IPv4; additional fields may be present for
        IPv6). If a ``callback`` is passed, it will be run with the
        result as an argument when it is complete.
        """
        raise NotImplementedError()

    def close(self):
        """Closes the `Resolver`, freeing any resources used.

        .. versionadded:: 3.1

        """
        pass


class ExecutorResolver(Resolver):
    """Resolver implementation using a `concurrent.futures.Executor`.

    Use this instead of `ThreadedResolver` when you require additional
    control over the executor being used.

    The executor will be shut down when the resolver is closed unless
    ``close_resolver=False``; use this if you want to reuse the same
    executor elsewhere.
    """
    def initialize(self, io_loop=None, executor=None, close_executor=True):
        self.io_loop = io_loop or IOLoop.current()
        if executor is not None:
            self.executor = executor
            self.close_executor = close_executor
        else:
            self.executor = dummy_executor
            self.close_executor = False

    def close(self):
        if self.close_executor:
            self.executor.shutdown()
        self.executor = None

    @run_on_executor
    def resolve(self, host, port, family=socket.AF_UNSPEC):
        # On Solaris, getaddrinfo fails if the given port is not found
        # in /etc/services and no socket type is given, so we must pass
        # one here.  The socket type used here doesn't seem to actually
        # matter (we discard the one we get back in the results),
        # so the addresses we return should still be usable with SOCK_DGRAM.
        addrinfo = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
        results = []
        for family, socktype, proto, canonname, address in addrinfo:
            results.append((family, address))
        return results


class BlockingResolver(ExecutorResolver):
    """Default `Resolver` implementation, using `socket.getaddrinfo`.

    The `.IOLoop` will be blocked during the resolution, although the
    callback will not be run until the next `.IOLoop` iteration.
    """
    def initialize(self, io_loop=None):
        super(BlockingResolver, self).initialize(io_loop=io_loop)


class ThreadedResolver(ExecutorResolver):
    """Multithreaded non-blocking `Resolver` implementation.

    Requires the `concurrent.futures` package to be installed
    (available in the standard library since Python 3.2,
    installable with ``pip install futures`` in older versions).

    The thread pool size can be configured with::

        Resolver.configure('tornado.netutil.ThreadedResolver',
                           num_threads=10)

    .. versionchanged:: 3.1
       All ``ThreadedResolvers`` share a single thread pool, whose
       size is set by the first one to be created.
    """
    _threadpool = None
    _threadpool_pid = None

    def initialize(self, io_loop=None, num_threads=10):
        threadpool = ThreadedResolver._create_threadpool(num_threads)
        super(ThreadedResolver, self).initialize(
            io_loop=io_loop, executor=threadpool, close_executor=False)

    @classmethod
    def _create_threadpool(cls, num_threads):
        pid = os.getpid()
        if cls._threadpool_pid != pid:
            # Threads cannot survive after a fork, so if our pid isn't what it
            # was when we created the pool then delete it.
            cls._threadpool = None
        if cls._threadpool is None:
            from concurrent.futures import ThreadPoolExecutor
            cls._threadpool = ThreadPoolExecutor(num_threads)
            cls._threadpool_pid = pid
        return cls._threadpool


class OverrideResolver(Resolver):
    """Wraps a resolver with a mapping of overrides.

    This can be used to make local DNS changes (e.g. for testing)
    without modifying system-wide settings.

    The mapping can contain either host strings or host-port pairs.
    """
    def initialize(self, resolver, mapping):
        self.resolver = resolver
        self.mapping = mapping

    def close(self):
        self.resolver.close()

    def resolve(self, host, port, *args, **kwargs):
        if (host, port) in self.mapping:
            host, port = self.mapping[(host, port)]
        elif host in self.mapping:
            host = self.mapping[host]
        return self.resolver.resolve(host, port, *args, **kwargs)


# These are the keyword arguments to ssl.wrap_socket that must be translated
# to their SSLContext equivalents (the other arguments are still passed
# to SSLContext.wrap_socket).
_SSL_CONTEXT_KEYWORDS = frozenset(['ssl_version', 'certfile', 'keyfile',
                                   'cert_reqs', 'ca_certs', 'ciphers'])


def ssl_options_to_context(ssl_options):
    """Try to convert an ``ssl_options`` dictionary to an
    `~ssl.SSLContext` object.

    The ``ssl_options`` dictionary contains keywords to be passed to
    `ssl.wrap_socket`.  In Python 3.2+, `ssl.SSLContext` objects can
    be used instead.  This function converts the dict form to its
    `~ssl.SSLContext` equivalent, and may be used when a component which
    accepts both forms needs to upgrade to the `~ssl.SSLContext` version
    to use features like SNI or NPN.
    """
    if isinstance(ssl_options, dict):
        assert all(k in _SSL_CONTEXT_KEYWORDS for k in ssl_options), ssl_options
    if (not hasattr(ssl, 'SSLContext') or
            isinstance(ssl_options, ssl.SSLContext)):
        return ssl_options
    context = ssl.SSLContext(
        ssl_options.get('ssl_version', ssl.PROTOCOL_SSLv23))
    if 'certfile' in ssl_options:
        context.load_cert_chain(ssl_options['certfile'], ssl_options.get('keyfile', None))
    if 'cert_reqs' in ssl_options:
        context.verify_mode = ssl_options['cert_reqs']
    if 'ca_certs' in ssl_options:
        context.load_verify_locations(ssl_options['ca_certs'])
    if 'ciphers' in ssl_options:
        context.set_ciphers(ssl_options['ciphers'])
    return context


def ssl_wrap_socket(socket, ssl_options, server_hostname=None, **kwargs):
    """Returns an ``ssl.SSLSocket`` wrapping the given socket.

    ``ssl_options`` may be either a dictionary (as accepted by
    `ssl_options_to_context`) or an `ssl.SSLContext` object.
    Additional keyword arguments are passed to ``wrap_socket``
    (either the `~ssl.SSLContext` method or the `ssl` module function
    as appropriate).
    """
    context = ssl_options_to_context(ssl_options)
    if hasattr(ssl, 'SSLContext') and isinstance(context, ssl.SSLContext):
        if server_hostname is not None and getattr(ssl, 'HAS_SNI'):
            # Python doesn't have server-side SNI support so we can't
            # really unittest this, but it can be manually tested with
            # python3.2 -m tornado.httpclient https://sni.velox.ch
            return context.wrap_socket(socket, server_hostname=server_hostname,
                                       **kwargs)
        else:
            return context.wrap_socket(socket, **kwargs)
    else:
        return ssl.wrap_socket(socket, **dict(context, **kwargs))

if hasattr(ssl, 'match_hostname') and hasattr(ssl, 'CertificateError'):  # python 3.2+
    ssl_match_hostname = ssl.match_hostname
    SSLCertificateError = ssl.CertificateError
else:
    # match_hostname was added to the standard library ssl module in python 3.2.
    # The following code was backported for older releases and copied from
    # https://bitbucket.org/brandon/backports.ssl_match_hostname
    class SSLCertificateError(ValueError):
        pass

    def _dnsname_to_pat(dn, max_wildcards=1):
        pats = []
        for frag in dn.split(r'.'):
            if frag.count('*') > max_wildcards:
                # Issue #17980: avoid denials of service by refusing more
                # than one wildcard per fragment.  A survery of established
                # policy among SSL implementations showed it to be a
                # reasonable choice.
                raise SSLCertificateError(
                    "too many wildcards in certificate DNS name: " + repr(dn))
            if frag == '*':
                # When '*' is a fragment by itself, it matches a non-empty dotless
                # fragment.
                pats.append('[^.]+')
            else:
                # Otherwise, '*' matches any dotless fragment.
                frag = re.escape(frag)
                pats.append(frag.replace(r'\*', '[^.]*'))
        return re.compile(r'\A' + r'\.'.join(pats) + r'\Z', re.IGNORECASE)

    def ssl_match_hostname(cert, hostname):
        """Verify that *cert* (in decoded format as returned by
        SSLSocket.getpeercert()) matches the *hostname*.  RFC 2818 rules
        are mostly followed, but IP addresses are not accepted for *hostname*.

        CertificateError is raised on failure. On success, the function
        returns nothing.
        """
        if not cert:
            raise ValueError("empty or no certificate")
        dnsnames = []
        san = cert.get('subjectAltName', ())
        for key, value in san:
            if key == 'DNS':
                if _dnsname_to_pat(value).match(hostname):
                    return
                dnsnames.append(value)
        if not dnsnames:
            # The subject is only checked when there is no dNSName entry
            # in subjectAltName
            for sub in cert.get('subject', ()):
                for key, value in sub:
                    # XXX according to RFC 2818, the most specific Common Name
                    # must be used.
                    if key == 'commonName':
                        if _dnsname_to_pat(value).match(hostname):
                            return
                        dnsnames.append(value)
        if len(dnsnames) > 1:
            raise SSLCertificateError("hostname %r "
                                      "doesn't match either of %s"
                                      % (hostname, ', '.join(map(repr, dnsnames))))
        elif len(dnsnames) == 1:
            raise SSLCertificateError("hostname %r "
                                      "doesn't match %r"
                                      % (hostname, dnsnames[0]))
        else:
            raise SSLCertificateError("no appropriate commonName or "
                                      "subjectAltName fields were found")

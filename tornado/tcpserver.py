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

"""A non-blocking, single-threaded TCP server."""
from __future__ import absolute_import, division, print_function, with_statement

import errno
import os
import socket
import ssl

from tornado.log import app_log
from tornado.ioloop import IOLoop
from tornado.iostream import IOStream, SSLIOStream
from tornado.netutil import bind_sockets, add_accept_handler, ssl_wrap_socket
from tornado import process


class TCPServer(object):
    r"""A non-blocking, single-threaded TCP server.

    To use `TCPServer`, define a subclass which overrides the `handle_stream`
    method.

    To make this server serve SSL traffic, send the ssl_options dictionary
    argument with the arguments required for the `ssl.wrap_socket` method,
    including "certfile" and "keyfile"::

       TCPServer(ssl_options={
           "certfile": os.path.join(data_dir, "mydomain.crt"),
           "keyfile": os.path.join(data_dir, "mydomain.key"),
       })

    `TCPServer` initialization follows one of three patterns:

    1. `listen`: simple single-process::

            server = TCPServer()
            server.listen(8888)
            IOLoop.instance().start()

    2. `bind`/`start`: simple multi-process::

            server = TCPServer()
            server.bind(8888)
            server.start(0)  # Forks multiple sub-processes
            IOLoop.instance().start()

       When using this interface, an `.IOLoop` must *not* be passed
       to the `TCPServer` constructor.  `start` will always start
       the server on the default singleton `.IOLoop`.

    3. `add_sockets`: advanced multi-process::

            sockets = bind_sockets(8888)
            tornado.process.fork_processes(0)
            server = TCPServer()
            server.add_sockets(sockets)
            IOLoop.instance().start()

       The `add_sockets` interface is more complicated, but it can be
       used with `tornado.process.fork_processes` to give you more
       flexibility in when the fork happens.  `add_sockets` can
       also be used in single-process servers if you want to create
       your listening sockets in some way other than
       `~tornado.netutil.bind_sockets`.

    .. versionadded:: 3.1
       The ``max_buffer_size`` argument.
    """

    # non-blocking，就是说，这个服务器没有使用阻塞式API。
    # 这里讨论一个问题就是阻塞与非阻塞
    # 那么什么是阻塞式设计呢？
    # 举个例子，在BSD Socket里，recv函数默认是阻塞式的。使用recv读取客户端数据时，如果对方并未发送数据，
    # 则这个API就会一直阻塞那里不返回。这样服务器的设计【不得不使用】多线程或者多进程方式，
    # 避免因为一个API的阻塞导致服务器没法做其它事。阻塞式API是很常见的，我们可以简单认为，
    # 阻塞式设计就是“不管有没有数据，服务器都派API去读，读不到，API就不会回来交差”

    # 何为非阻塞设计？
    # 而非阻塞，对recv来说，区别在于没有数据可读时，它不会在那死等，它直接就返回了。
    # 你可能会认为这办法比阻塞式还要矬，因为服务器无法预知有没有数据可读，不得不反复派recv函数去读。
    # 这不是浪费大量的CPU资源么？
    # 当然不会这么傻。tornado这里说的非阻塞要高级得多，
    # 基本上是另一种思路：服务器并【不主动】读取数据，它和操作系统合作，实现了一种“监视器”，
    # TCP连接就是它的监视对象。当某个连接上有数据到来时，操作系统会按事先的约定通知服务器：某某号连接上有数据到来，
    # 你去处理一下。服务器这时候才派API去取数据。服务器不用创建大量线程来阻塞式的处理每个连接，
    # 也不用不停派API去检查连接上有没有数据，它只需要坐那里等操作系统的通知，这保证了recv API出手就不会落空。

    # tornado的牛逼之处：
    # tornado另一个被强调的特征是single-threaded，这是因为我们的“监视器”非常高效，
    # 可以在【一个线程里】监视【成千上万个】连接的状态，基本上不需要再动用线程来分流。
    # 实测表明，它比阻塞式多线程或者多进程设计更加高效——当然，这依赖于操作系统的大力配合，
    # 现在主流操作系统都提供了非常高端大气上档次的“监视器”机制，比如epoll、kqueue。

    # 作者提到这个类一般不直接被实例化，而是由它派生出子类，再用子类实例化。
    # 为了强化这个设计思想，作者定义了一个未直接实现的接口，叫handle_stream()。
    # def handle_stream(self, stream, address):
    #     """Override to handle a new `.IOStream` from an incoming connection."""
    #     raise NotImplementedError()


    def __init__(self, io_loop=None, ssl_options=None, max_buffer_size=None):

        # TCPServer 类的定义在 tcpserver.py。它有两种用法：bind+start 或者 listen。
        # 第一种用法可用于多线程，但在 TCP 方面两者是一样的。就以 【listen】为例吧。
        # TCPServer 的__init__没什么注意的，就是记住了 【ioloop】 这个单例，
        # 这个下节再分析（它是tornado异步性能的关键）
        # TCPServer的__init__函数很简单，仅保存了参数而已。
        # 唯一要注意的是，它可以接受一个io_loop为参数。实际上io_loop对TCPServer来说【并不是可有可无】，
        # 它是【必须的】。不过TCPServer提供了多种渠道来与一个io_loop绑定，初始化参数只是其中一种绑定方式而已。

        #
        # IO循环
        self.io_loop = io_loop
        # Http和Http
        self.ssl_options = ssl_options
        self._sockets = {}  # fd -> socket object（socket对象）
        self._pending_sockets = []
        self._started = False
        self.max_buffer_size = max_buffer_size

        # Verify the SSL options. Otherwise we don't get errors until clients
        # connect. This doesn't verify that the keys are legitimate, but
        # the SSL module doesn't do that until there is a connected socket
        # which seems like too much work
        if self.ssl_options is not None and isinstance(self.ssl_options, dict):
            # Only certfile is required: it can contain both keys
            if 'certfile' not in self.ssl_options:
                raise KeyError('missing key "certfile" in ssl_options')

            if not os.path.exists(self.ssl_options['certfile']):
                raise ValueError('certfile "%s" does not exist' %
                                 self.ssl_options['certfile'])
            if ('keyfile' in self.ssl_options and
                    not os.path.exists(self.ssl_options['keyfile'])):
                raise ValueError('keyfile "%s" does not exist' %


                                 self.ssl_options['keyfile'])
    # 开始接受给定端口上的连接。
    def listen(self, port, address=""):
        """Starts accepting connections on the given port.

        This method may be called more than once to listen on multiple ports.
        `listen` takes effect immediately; it is not necessary to call
        `TCPServer.start` afterwards.  It is, however, necessary to start
        the `.IOLoop`.
        """

        # 此方法可以不止一次地调用多个端口。
        # “听”立即生效，没有必要通知` tcpserver开始`。然而【开始】ioloop这是必要的。


        # TCPServer.bind_sockets()会返回一个socket对象的【列表】，
        # 列表中的socket【都是】用来监听客户端连接的。

        # 列表由TCPServer.add_sockets()处理。在这个函数里我们就会看到IOLoop相关的东西。

        # 首先，io_loop是TCPServer的一个【成员变量】，这说明每个TCPServer都绑定了一个io_loop。
        # [注意]，跟传统的做法不同，【ioloop不是跟socket一一对应，而是跟TCPServer一一对应。】
        # 也就是说，一个Server上即使有多个listening socket，他们也是由【同一个ioloop在处理】。

        # 前面提到过，HTTPServer的初始化可以带一个ioloop参数，
        # 最终它会被赋值给TCPServer的成员。是因为：(TCPServer.__init__(self, io_loop=io_loop,
        # ssl_options=ssl_options, **kwargs))
        # 如果没有带ioloop参数（如helloworld.py所展示的），
        # 【TCPServer则会自己倒腾一个】，即IOLoop.current()。
        # (add_accept_handler()定义在netutil.py中（bind_sockets在这里）)


        # listen 方法接收两个参数：端口和地址
        # 整体分析：
        # 首先 bind_sockets 方法接收地址和端口创建 sockets 列表并绑定地址端口【并监听】
        # （完成了TCP三部曲的前两部）
        # bind_socket完成的工作包括：创建socket，绑定socket到指定的地址和端口，开启侦听。

        # 创建绑定到给定端口和地址的监听套接字。
        sockets = bind_sockets(port, address=address)
        # add_sockets 在这些 sockets 上注册 read/timeout 事件。
        self.add_sockets(sockets)  # 使此服务器开始接受给定套接字上的连接。

        # 有关高性能并发服务器编程可以参照UNIX网络编程里给的几种编程模型，
        # tornado 可以看作是单线程事件驱动模式的服务器，TCP 三部曲中的第三部就被分隔到了事件回调里，
        # 因此肯定要在所有的文件 fd（包括sockets）上监听事件。在做完这些事情后就可以安心的调用 ioloop
        # 单例的 start 方法开始循环监听事件了。具体细节可以参照现代高性能 web 服务器(nginx/lightttpd等)
        # 的事件模型，后面也会涉及一点。

        # 简言之，基于【事件驱动】的服务器（tornado）要干的事就是：
        # 创建 socket，绑定到端口并 listen，然后注册事件和对应的回调，在回调里accept 新请求。

        # TCPServer类的listen函数是开始接受【指定端口上的连接】。注意，这个listen与BSD Socket中的listen并不等价，
        # 它做的事比BSD socket()+bind()+listen()还要多。
        # 注意在函数注释中提到的一句话：你可以在一个server的实例中多次调用listen，以实现一个server侦听多个端口。
        # 怎么理解？在BSD Socket架构里，我们不可能在一个socket上同时侦听多个端口。反推之，不难想到，
        # TCPServer的listen函数内部一定是执行了全套的BSD Socket三段式（create socket->bind->listen），
        # 使得【每调用一次listen】实际上是【创建了一个新的socket】。
        # 代码很好的符合了我们的猜想：
        # 第一、先创建了一个socket，然后把它加到自己的侦听队列里。
        # 第二、


    def add_sockets(self, sockets):
        """Makes this server start accepting connections on the given sockets.

        The ``sockets`` parameter is a list of socket objects such as
        those returned by `~tornado.netutil.bind_sockets`.
        `add_sockets` is typically used in combination with that
        method and `tornado.process.fork_processes` to provide greater
        control over the initialization of a multi-process server.
        """

        # add_sockets 方法接收 socket 列表，对于列表中的 socket，
        # 用 fd 【作键】记录下来，并调用add_accept_handler 方法。它也是在 netutil.py 里定义的，
        if self.io_loop is None:
            # 1.Returns the current thread's `IOLoop`.
            # 2.Returns a global `IOLoop` instance.
            self.io_loop = IOLoop.current()

        for sock in sockets:
            # 循环将每一个sock添加到IOLoop中
            # sock.fileno() -> self.socket.fileno() -> self.fd
            self._sockets[sock.fileno()] = sock
            # 同时添加回调函数_handle_connection ；

            # IOLoop添加对应的socket的IOLoop.READ事件监听
            add_accept_handler(sock, self._handle_connection,
                               io_loop=self.io_loop)

        # callback就是我们传进去的TCPServer._handle_connection()函数。
        # TCPServer._handle_connection()本身不复杂。前面大段都是在处理SSL事务，
        # 把这些东西都滤掉的话，实际上代码很少。

    def add_socket(self, socket):
        """Singular version of `add_sockets`.  Takes a single socket object."""
        self.add_sockets([socket])

    def bind(self, port, address=None, family=socket.AF_UNSPEC, backlog=128):
        """Binds this server to the given port on the given address.

        To start the server, call `start`. If you want to run this server
        in a single process, you can call `listen` as a shortcut to the
        sequence of `bind` and `start` calls.

        Address may be either an IP address or hostname.  If it's a hostname,
        the server will listen on all IP addresses associated with the
        name.  Address may be an empty string or None to listen on all
        available interfaces.  Family may be set to either `socket.AF_INET`
        or `socket.AF_INET6` to restrict to IPv4 or IPv6 addresses, otherwise
        both will be used if available.

        The ``backlog`` argument has the same meaning as for
        `socket.listen <socket.socket.listen>`.

        This method may be called multiple times prior to `start` to listen
        on multiple ports or interfaces.
        """
        sockets = bind_sockets(port, address=address, family=family,
                               backlog=backlog)
        if self._started:
            self.add_sockets(sockets)
        else:
            self._pending_sockets.extend(sockets)

    def start(self, num_processes=1):
        """Starts this server in the `.IOLoop`.

        By default, we run the server in this process and do not fork any
        additional child process.

        If num_processes is ``None`` or <= 0, we detect the number of cores
        available on this machine and fork that number of child
        processes. If num_processes is given and > 1, we fork that
        specific number of sub-processes.

        Since we use processes and not threads, there is no shared memory
        between any server code.

        Note that multiple processes are not compatible with the autoreload
        module (or the ``debug=True`` option to `tornado.web.Application`).
        When using multiple processes, no IOLoops can be created or
        referenced until after the call to ``TCPServer.start(n)``.
        """
        assert not self._started
        self._started = True
        # 如果进程数大于1;(你等于1)
        if num_processes != 1:
            process.fork_processes(num_processes)
        # 进程数等于1，默认
        sockets = self._pending_sockets
        self._pending_sockets = []
        self.add_sockets(sockets)

    def stop(self):
        """Stops listening for new connections.

        Requests currently in progress may still continue after the
        server is stopped.
        """
        for fd, sock in self._sockets.items():
            self.io_loop.remove_handler(fd)
            sock.close()

    def handle_stream(self, stream, address):
        """Override to handle a new `.IOStream` from an incoming connection."""
        raise NotImplementedError()

    def _handle_connection(self, connection, address):

        # _handle_connection就比较简单了，跳过那些ssl的处理，简化为两句：
        # stream = IOStream(connection, io_loop=self.io_loop)和
        # self.handle_stream()。
        # 这里IOStream代表了【IO层】(也就是读写方面的东西)，以后再说，反正读写是不愁了。
        # 接着是调用handle_stream。我们可以看到，不论应用层是什么协议
        # （或者自定义协议），当有新连接到来时走的流程是差不多的，都要经历一番上诉的回调，
        # 不同之处就在于这个handle_stream方法。这个方法是由【子类自定义覆盖的】，
        # 它的HTTP实现已经在上一节看过了。

        #

        if self.ssl_options is not None:
            assert ssl, "Python 2.6+ and OpenSSL required for SSL"
            try:
                connection = ssl_wrap_socket(connection,
                                             self.ssl_options,
                                             server_side=True,
                                             do_handshake_on_connect=False)
            except ssl.SSLError as err:
                if err.args[0] == ssl.SSL_ERROR_EOF:
                    return connection.close()
                else:
                    raise
            except socket.error as err:
                # If the connection is closed immediately after it is created
                # (as in a port scan), we can get one of several errors.
                # wrap_socket makes an internal call to getpeername,
                # which may return either EINVAL (Mac OS X) or ENOTCONN
                # (Linux).  If it returns ENOTCONN, this error is
                # silently swallowed by the ssl module, so we need to
                # catch another error later on (AttributeError in
                # SSLIOStream._do_ssl_handshake).
                # To test this behavior, try nmap with the -sT flag.
                # https://github.com/facebook/tornado/pull/750
                if err.args[0] in (errno.ECONNABORTED, errno.EINVAL):
                    return connection.close()
                else:
                    raise
        try:
            if self.ssl_options is not None:
                stream = SSLIOStream(connection, io_loop=self.io_loop, max_buffer_size=self.max_buffer_size)
            else:
                stream = IOStream(connection, io_loop=self.io_loop, max_buffer_size=self.max_buffer_size)

            # 调用handle_stream，传入创建的IOStream对象初始化一个HTTPConnection对象，
            # HTTPConnection封装了IOStream的一些操作，用于处理HTTPRequest并返回。

            # 1.1.0 中的写法：
            # ====important=====#
            # HTTPConnection(stream, address, self.request_callback, self.no_keep_alive, self.xheaders)

            self.handle_stream(stream, address)

        # 思路是很清晰的，客户端连接在这里被转化成一个IOStream。然后由handle_stream函数处理。
        # 这个handle_stream就是我们前面提到过的未直接实现的接口，它是由HTTPServer类实现的。
        # def handle_stream(self, stream, address):
        #     HTTPConnection(stream, address, self.request_callback,
        #         self.no_keep_alive, self.xheaders, self.protocol)
        # 最后，处理流程又回到了HTTPServer类中。可以预见，在HTTConnection这个类中，
        # stream将和我们注册的RequestHandler协作，一边读客户端请求，一边调用相应的handler处理。


        except Exception:
            app_log.error("Error in connection callback", exc_info=True)


        # 总结一下：
        # listening socket创建起来以后，我们给它绑定一个响应函数叫accept_handler。
        # 当用户连接上来时，会触发listening socket上的事件，然后accept_handler被调用。
        # accept_handler在listening socket上获得一个针对该用户的新socket。
        # 这个socket专门用来跟用户做数据沟通。TCPServer把这个socket封闭成一个IOStream，
        # 最后交到HTTPServer的handle_stream里。
        # 经过一翻周围，用户连接后的HTTP通讯终于被我们导入到了HTTPConnection。
        # 在HTTPConnection里我们将看到熟悉的HTTP【通信协议】的处理。
        # HTTPConnection类也定义在httpserver.py中，它的构造函数如下：

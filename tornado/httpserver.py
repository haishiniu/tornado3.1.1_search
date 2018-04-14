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

"""A non-blocking, single-threaded HTTP server.

Typical applications have little direct interaction with the `HTTPServer`
class except to start a server at the beginning of the process
(and even that is often done indirectly via `tornado.web.Application.listen`).

This module also defines the `HTTPRequest` class which is exposed via
`tornado.web.RequestHandler.request`.
"""

from __future__ import absolute_import, division, print_function, with_statement

import socket
import ssl
import time

from tornado.escape import native_str, parse_qs_bytes
from tornado import httputil
from tornado import iostream
from tornado.log import gen_log
from tornado import netutil
from tornado.tcpserver import TCPServer
from tornado import stack_context
from tornado.util import bytes_type

try:
    import Cookie  # py2
except ImportError:
    import http.cookies as Cookie  # py3


class HTTPServer(TCPServer):
    r"""A non-blocking, single-threaded HTTP server.

    A server is defined by a request callback that takes an HTTPRequest
    instance as an argument and writes a valid HTTP response with
    `HTTPRequest.write`. `HTTPRequest.finish` finishes the request (but does
    not necessarily close the connection in the case of HTTP/1.1 keep-alive
    requests). A simple example server that echoes back the URI you
    requested::

        import tornado.httpserver
        import tornado.ioloop

        def handle_request(request):
           message = "You requested %s\n" % request.uri
           request.write("HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (
                         len(message), message))
           request.finish()

        http_server = tornado.httpserver.HTTPServer(handle_request)
        http_server.listen(8888)
        tornado.ioloop.IOLoop.instance().start()

    `HTTPServer` is a very basic connection handler.  It parses the request
    headers and body, but the request callback is responsible for producing
    the response exactly as it will appear on the wire.  This affords
    maximum flexibility for applications to implement whatever parts
    of HTTP responses are required.

    `HTTPServer` supports keep-alive connections by default
    (automatically for HTTP/1.1, or for HTTP/1.0 when the client
    requests ``Connection: keep-alive``).  This means that the request
    callback must generate a properly-framed response, using either
    the ``Content-Length`` header or ``Transfer-Encoding: chunked``.
    Applications that are unable to frame their responses properly
    should instead return a ``Connection: close`` header in each
    response and pass ``no_keep_alive=True`` to the `HTTPServer`
    constructor.

    If ``xheaders`` is ``True``, we support the
    ``X-Real-Ip``/``X-Forwarded-For`` and
    ``X-Scheme``/``X-Forwarded-Proto`` headers, which override the
    remote IP and URI scheme/protocol for all requests.  These headers
    are useful when running Tornado behind a reverse proxy or load
    balancer.  The ``protocol`` argument can also be set to ``https``
    if Tornado is run behind an SSL-decoding proxy that does not set one of
    the supported ``xheaders``.

    To make this server serve SSL traffic, send the ``ssl_options`` dictionary
    argument with the arguments required for the `ssl.wrap_socket` method,
    including ``certfile`` and ``keyfile``.  (In Python 3.2+ you can pass
    an `ssl.SSLContext` object instead of a dict)::

       HTTPServer(applicaton, ssl_options={
           "certfile": os.path.join(data_dir, "mydomain.crt"),
           "keyfile": os.path.join(data_dir, "mydomain.key"),
       })

    `HTTPServer` initialization follows one of three patterns (the
    initialization methods are defined on `tornado.tcpserver.TCPServer`):

    1. `~tornado.tcpserver.TCPServer.listen`: simple single-process::

            server = HTTPServer(app)
            server.listen(8888)
            IOLoop.instance().start()

       In many cases, `tornado.web.Application.listen` can be used to avoid
       the need to explicitly create the `HTTPServer`.

    2. `~tornado.tcpserver.TCPServer.bind`/`~tornado.tcpserver.TCPServer.start`:
       simple multi-process::

            server = HTTPServer(app)
            server.bind(8888)
            server.start(0)  # Forks multiple sub-processes
            IOLoop.instance().start()

       When using this interface, an `.IOLoop` must *not* be passed
       to the `HTTPServer` constructor.  `~.TCPServer.start` will always start
       the server on the default singleton `.IOLoop`.

    3. `~tornado.tcpserver.TCPServer.add_sockets`: advanced multi-process::

            sockets = tornado.netutil.bind_sockets(8888)
            tornado.process.fork_processes(0)
            server = HTTPServer(app)
            server.add_sockets(sockets)
            IOLoop.instance().start()

       The `~.TCPServer.add_sockets` interface is more complicated,
       but it can be used with `tornado.process.fork_processes` to
       give you more flexibility in when the fork happens.
       `~.TCPServer.add_sockets` can also be used in single-process
       servers if you want to create your listening sockets in some
       way other than `tornado.netutil.bind_sockets`.

    """
    def __init__(self, request_callback, no_keep_alive=False, io_loop=None,
                 xheaders=False, ssl_options=None, protocol=None, **kwargs):
        # __init__ 记录了链接到来时的回调函数(http层面的回调)
        # 亦即application对象(它的__call__方法会被触发)，
        # 然后就是父类的初始化了。
        # TCP 服务器细节后面再看，简言之，它可以：
        # 它可以监听在特定地址-端口上，并每当有客户端发起连接到服务器时接收该连接，
        # 并调用方法 handle_stream（TCP 层次的回调，这个方法总是【被子类覆盖】，
        # 因为只有在这里才可以实现不同应用层协议的业务逻辑）。


        #
        # Application对象

        # （request_callback中）包含了各种配置信息的【application对象】

        self.request_callback = request_callback
        # 是否长连接
        self.no_keep_alive = no_keep_alive
        self.xheaders = xheaders
        self.protocol = protocol
        TCPServer.__init__(self, io_loop=io_loop, ssl_options=ssl_options,
                           **kwargs)

    # handle_stream方法。这个方法是由子类自定义覆盖的，
    #
    def handle_stream(self, stream, address):
        # HTTPServer 覆盖了父类的 handler_stream 方法，并在该方法里生成 HTTPConnection 对象就结束了。
        # 由此可知，HTTPConnection对象被构建就立即开始了 http 协议的处理，这样是合理的，
        # 因为 handle_stream 被调用的时候肯定是新连接到来，这时缓冲区里一般有数据可读，
        # 当然可以直接读取并处理。
        HTTPConnection(stream, address, self.request_callback,
                       self.no_keep_alive, self.xheaders, self.protocol)


class _BadRequestException(Exception):
    """Exception class for malformed HTTP requests."""
    pass


# HTTPConnection 里的方法，大部分都可以顾名思义
# 首先是__init__, 它也没干什么，初始化协议参数和回调函数的默认值（一般是None）。
# 然后设定了 header_callback，并开始读取

class HTTPConnection(object):
    """Handles a connection to an HTTP client, executing HTTP requests.

    We parse HTTP headers and bodies, and execute the request callback
    until the HTTP conection is closed.
    """
    # 该类用于[处理http请求]

    # HTTPConnection 是 HTTP 协议在服务端的真正实现者，我的意思是说，对于请求的读取和解析，
    # 基本是由它（依靠HTTPHeaders）完成的。但是响应方面的处理（包括响应头，响应主体的中间处理等）则是在
    # RequestHandler 里的 flush 方法和 finish 方法里完成的。

    def __init__(self, stream, address, request_callback, no_keep_alive=False,
                 xheaders=False, protocol=None):

        # 在HTTPConnection【初始化时】
        # 对self.request_callback【赋值】为一个【可调用的对象】
        # （该对象用于对http请求的【具体处理和应答】）。
        # 该类首先：
        # 读取http请求中header的结束符b("\r\n\r\n")，
        # 然后回调self._on_headers函数
        self.stream = stream  # stream是封装了客户端socket和IOLoop实例的IOStream对象
        self.address = address  # #address是客户端IP地址
        # Save the socket's address family now so we know how to
        # interpret self.address even after the stream is closed
        # and its socket attribute replaced with None.
        self.address_family = stream.socket.family
        self.request_callback = request_callback  # request_callback是封装了URL映射和配置文件的Application对象。
        self.no_keep_alive = no_keep_alive
        self.xheaders = xheaders
        self.protocol = protocol
        self._clear_request_state()
        # Save stack context here, outside of any request.  This keeps
        # contexts from one request from leaking into the next.

        # 该类首先
        # 读取http请求中header的结束符b("\r\n\r\n")，
        # 配置文件
        # 然后这里涉及到两个：stack_context.wrap和stream.read_until
        # 第一:先是这个wrap函数，它就是一个【装饰器】，就是封装了一下，对【多线程执行环境的上下文】做了一些维护。
        # 第二:read_until了，顾名思义，就是一直读取直到\r\n\r\n（这一般意味这请求头的结束），
        # 然后调用回调函数_on_headers（这是 IOStream 层次的回调）。
        # 具体怎么做的以后再说了，先确认是这么个功能。
        #
        # 然后 _on_headers 函数在请求头结束时被调用，
        # 它的参数只有一个 data，亦即读取到的请求头字符串。

        # 经过stack_context.wrap()的传递，HTTP头会交给HTTPConnection._on_headers()。
        # HTTPConnection._on_headers()完成了HTTP头的分析，
        # 具体过程这里就不必详述了（如果有写HTTPServer需求，倒是可以借鉴一下）。

        #  经过一轮校验与分析，HTTP头（注意，只是HTTP头部哦）被组装成一个HTTPRequest对象。
        # （HTTP Body如果数据量不是太大，就直接放进了一个buffer里，就叫：self._on_request_body。）

        #
        # stack_context.wrap其实就是对函数进行一下封装，即：函数在不同情况下上下文信息可能不同。

        # 获取请求信息（请求头和内容），然后执行 HTTPConnection的_on_headers方法继续处理请求
        self._header_callback = stack_context.wrap(self._on_headers)
        self.stream.set_close_callback(self._on_connection_close)
        # 常规的参数保存动作，还有一些初始化、清理动作，最后一句开始办正事：
        # 从socket中读数据，直到读到”\r\n\r\n”为止。这是HTTP头部结束标志，
        # 读到的数据就会由self._header_callback处理。（self._header_callback：相当于一个回调函数）

        #相当于执行def _on_headers()函数
        self.stream.read_until(b"\r\n\r\n", self._header_callback)

    def _clear_request_state(self):
        """Clears the per-request state.

        This is run in between requests to allow the previous handler
        to be garbage collected (and prevent spurious close callbacks),
        and when the connection is closed (to break up cycles and
        facilitate garbage collection in cpython).
        """
        self._request = None
        self._request_finished = False
        self._write_callback = None
        self._close_callback = None

    def set_close_callback(self, callback):
        """Sets a callback that will be run when the connection is closed.

        Use this instead of accessing
        `HTTPConnection.stream.set_close_callback
        <.BaseIOStream.set_close_callback>` directly (which was the
        recommended approach prior to Tornado 3.0).
        """
        self._close_callback = stack_context.wrap(callback)

    def _on_connection_close(self):
        if self._close_callback is not None:
            callback = self._close_callback
            self._close_callback = None
            callback()
        # Delete any unfinished callbacks to break up reference cycles.
        self._header_callback = None
        self._clear_request_state()

    def close(self):
        self.stream.close()
        # Remove this reference to self, which would otherwise cause a
        # cycle and delay garbage collection of this connection.
        self._header_callback = None
        self._clear_request_state()

    def write(self, chunk, callback=None):
        """Writes a chunk of output to the stream."""
        if not self.stream.closed():
            self._write_callback = stack_context.wrap(callback)
            # 调用 IOStream的 write()方法
            self.stream.write(chunk, self._on_write_complete)

    def finish(self):
        """Finishes the request."""
        self._request_finished = True
        # No more data is coming, so instruct TCP to send any remaining
        # data immediately instead of waiting for a full packet or ack.
        self.stream.set_nodelay(True)
        if not self.stream.writing():
            self._finish_request()

    def _on_write_complete(self):
        # 更新客户端socket所在epoll中的状态为【READ】，以便之后执行3.24时关闭socket客户端
        if self._write_callback is not None:
            callback = self._write_callback
            self._write_callback = None
            callback()
        # _on_write_complete is enqueued on the IOLoop whenever the
        # IOStream's write buffer becomes empty, but it's possible for
        # another callback that runs on the IOLoop before it to
        # simultaneously write more data and finish the request.  If
        # there is still data in the IOStream, a future
        # _on_write_complete will be responsible for calling
        # _finish_request.
        if self._request_finished and not self.stream.writing():
            self._finish_request()

    def _finish_request(self):
        if self.no_keep_alive or self._request is None:
            # 代表断开连接
            disconnect = True
        else:
            # 获取连接的信息
            #Connection:keep-alive 代表连接是ok的
            connection_header = self._request.headers.get("Connection")
            if connection_header is not None:
                connection_header = connection_header.lower()
            if self._request.supports_http_1_1():
                disconnect = connection_header == "close"
            elif ("Content-Length" in self._request.headers
                    or self._request.method in ("HEAD", "GET")):
                disconnect = connection_header != "keep-alive"
            else:
                disconnect = True
        # 清除请求的状态信息
        self._clear_request_state()
        if disconnect:
            self.close()
            return
        try:
            # Use a try/except instead of checking stream.closed()
            # directly, because in some cases the stream doesn't discover
            # that it's closed until you try to read from it.
            self.stream.read_until(b"\r\n\r\n", self._header_callback)

            # Turn Nagle's algorithm back on, leaving the stream in its
            # default state for the next request.
            self.stream.set_nodelay(False)
        except iostream.StreamClosedError:
            self.close()

    # 然后 _on_headers 函数在请求头结束时被调用，
    # 它的参数只有一个 data，亦即读取到的请求头字符串。
    def _on_headers(self, data):
        try:
            # 首先是找到起始行：
            data = native_str(data.decode('latin1'))
            eol = data.find("\r\n")
            # 获取请求的起始行数据，例如：GET / HTTP/1.1
            start_line = data[:eol]
            try:
                # 然后是用空格分解【首行】来找到方法，uri和协议版本：
                # 请求方式、请求地址、http版本号
                method, uri, version = start_line.split(" ")
            except ValueError:
                raise _BadRequestException("Malformed HTTP request line")
            if not version.startswith("HTTP/"):
                raise _BadRequestException("Malformed HTTP version in HTTP Request-Line")
            try:
                # 接着依靠HTTPHeaders解析 剩余的请求头，返回一个字典
                # 把请求头信息包装到一个字典中。（不包括第一行）
                headers = httputil.HTTPHeaders.parse(data[eol:])
            except ValueError:
                # Probably from split() if there was no ':' in the line
                raise _BadRequestException("Malformed HTTP headers")

            # HTTPRequest wants an IP, not a full socket address
            # 然后设定 remote_ip（好像没什么用？）
            if self.address_family in (socket.AF_INET, socket.AF_INET6):
                remote_ip = self.address[0]
            else:
                # Unix (or other) socket; fake the remote address
                remote_ip = '0.0.0.0'

            # 接着创建 request 对象（这就是 RequestHandler 接收的那个 request），
            # 然后用 Content-Length 检查是否有请求体，
            # 如果没有则直接调用 HTTP 层次的回调（亦即 application 的__call__方法），
            # 如果有则读取指定长度的内容并跳到回调 _on_request_body, 当然最终还是会调用 application 对象。
            # 在 _on_request_body 方法里是调用 parse_body_arguments方法来完成【解析主体，
            # 请求头和请求体的解析】稍候再说。至此，执行流程就和 Application对象的接口与起到的作用 接上了。
            # 至于何时调用handle_stream，后面会说到。

            #
            # 把请求信息封装到一个HTTPRequest对象中
            # 注意：self._request = HTTPRequest，
            # HTTPRequest中封装了HTTPConnection
            # HTTPConnection中封装了stream和application
            # connection=self,此self就是HTTPConnection
            # headers=headers,就是HTTPHeader对象


            self._request = HTTPRequest(
                connection=self, method=method, uri=uri, version=version,
                headers=headers, remote_ip=remote_ip, protocol=self.protocol)

            # HTTPRequest对象被交给了（其实HTTP Body最后也是交给他【self.request_callback(self._request)】）
            # self.request_callback(self._request)

            # 从请求头中获取 Content-Length
            content_length = headers.get("Content-Length")
            if content_length:
                content_length = int(content_length)
                if content_length > self.stream.max_buffer_size:
                    raise _BadRequestException("Content-Length too long")
                if headers.get("Expect") == "100-continue":
                    self.stream.write(b"HTTP/1.1 100 (Continue)\r\n\r\n")
                self.stream.read_bytes(content_length, self._on_request_body)
                return

            # **************** 执行Application对象的 __call__ 方法，也就是路由系统的入口 *******************
            #
            self.request_callback(self._request)

            # 这个request_callback是什么来头呢？它是在HTTPConnection构造时传进来的参数。
            # 我们回到HTTPServer.handle_stream()
            # def handle_stream(self, stream, address):
            #     HTTPConnection(stream, address, self.request_callback,
            #        self.no_keep_alive, self.xheaders, self.protocol)

            # 它是一个HTTPServer类的成员，继续往回追来历：
            # def __init__(self, request_callback, no_keep_alive=False, io_loop=None,
            #  xheaders=False, ssl_options=None, protocol=None, **kwargs):
            #   self.request_callback = request_callback

            # Bingo！这就是HTTPServer初始化时传进来的那个RequestHandler。

            # 在helloworld.py里，我们看到的是：
            # application = tornado.web.Application([(r"/", MainHandler), ])
            # http_server = tornado.httpserver.HTTPServer(application)
            #
            # 在另一个例子里，我们看到的是：
            # def handle_request(request):
            #    message = "You requested %s\n" % request.uri
            #    request.write("HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (
            #                  len(message), message))
            #    request.finish()
            # http_server = tornado.httpserver.HTTPServer(handle_request)

            # 可见这个request_handler通吃【很多种类型】的参数，
            # 可以是一个Application类的对象，也可是一个简单的函数。
            # 如果是handler是简单函数，如上面的handle_request，这个很好理解，由一个函数处理HTTPRequest对象嘛。

            # 如果是一个Application对象，就有点奇怪了。我们能把一个对象作另一个对象的参数来呼叫吗？

            # Python中有一个有趣的语法，只要定义类型的时候，实现__call__【函数】，这个类型就成为可调用的。
            # 换句话说，我们可以把这个类的对象当作函数来使用，相当于【重载了】括号运算符。


        except _BadRequestException as e:
            gen_log.info("Malformed HTTP request from %s: %s",
                         self.address[0], e)
            self.close()
            return

    def _on_request_body(self, data):
        self._request.body = data
        if self._request.method in ("POST", "PATCH", "PUT"):
            httputil.parse_body_arguments(
                self._request.headers.get("Content-Type", ""), data,
                self._request.arguments, self._request.files)
        self.request_callback(self._request)


class HTTPRequest(object):
    """A single HTTP request.

    All attributes are type `str` unless otherwise noted.

    .. attribute:: method

       HTTP request method, e.g. "GET" or "POST"

    .. attribute:: uri

       The requested uri.

    .. attribute:: path

       The path portion of `uri`

    .. attribute:: query

       The query portion of `uri`

    .. attribute:: version

       HTTP version specified in request, e.g. "HTTP/1.1"

    .. attribute:: headers

       `.HTTPHeaders` dictionary-like object for request headers.  Acts like
       a case-insensitive dictionary with additional methods for repeated
       headers.

    .. attribute:: body

       Request body, if present, as a byte string.

    .. attribute:: remote_ip

       Client's IP address as a string.  If ``HTTPServer.xheaders`` is set,
       will pass along the real IP address provided by a load balancer
       in the ``X-Real-Ip`` or ``X-Forwarded-For`` header.

    .. versionchanged:: 3.1
       The list format of ``X-Forwarded-For`` is now supported.

    .. attribute:: protocol

       The protocol used, either "http" or "https".  If ``HTTPServer.xheaders``
       is set, will pass along the protocol used by a load balancer if
       reported via an ``X-Scheme`` header.

    .. attribute:: host

       The requested hostname, usually taken from the ``Host`` header.

    .. attribute:: arguments

       GET/POST arguments are available in the arguments property, which
       maps arguments names to lists of values (to support multiple values
       for individual names). Names are of type `str`, while arguments
       are byte strings.  Note that this is different from
       `.RequestHandler.get_argument`, which returns argument values as
       unicode strings.

    .. attribute:: files

       File uploads are available in the files property, which maps file
       names to lists of `.HTTPFile`.

    .. attribute:: connection

       An HTTP request is attached to a single HTTP connection, which can
       be accessed through the "connection" attribute. Since connections
       are typically kept open in HTTP/1.1, multiple requests can be handled
       sequentially on a single connection.
    """
    def __init__(self, method, uri, version="HTTP/1.0", headers=None,
                 body=None, remote_ip=None, protocol=None, host=None,
                 files=None, connection=None):

        # 简单提一下 HTTPRequest。它是【客户端请求的代表】，它携带了所有和客户端请求的信息，
        # 因为 application 的回调__call__方法【只】接收 request 参数，
        # 当然是把所有信息包在其中。另外，由于服务器只把 request 对象暴露给 application 的回调，
        # 因此request 对象还需要提供 write，finish 方法来提供服务，
        # 其实就是对 HTTPConnection 对象的【封装调用】。其它也没什么了。
        self.method = method
        self.uri = uri
        self.version = version
        self.headers = headers or httputil.HTTPHeaders()
        self.body = body or ""

        # set remote IP and protocol
        self.remote_ip = remote_ip
        if protocol:
            self.protocol = protocol
        elif connection and isinstance(connection.stream,
                                       iostream.SSLIOStream):
            self.protocol = "https"
        else:
            self.protocol = "http"

        # xheaders can override the defaults
        if connection and connection.xheaders:
            # Squid uses X-Forwarded-For, others use X-Real-Ip
            ip = self.headers.get("X-Forwarded-For", self.remote_ip)
            ip = ip.split(',')[-1].strip()
            ip = self.headers.get(
                "X-Real-Ip", ip)
            if netutil.is_valid_ip(ip):
                self.remote_ip = ip
            # AWS uses X-Forwarded-Proto
            proto = self.headers.get(
                "X-Scheme", self.headers.get("X-Forwarded-Proto", self.protocol))
            if proto in ("http", "https"):
                self.protocol = proto

        self.host = host or self.headers.get("Host") or "127.0.0.1"
        self.files = files or {}
        self.connection = connection
        self._start_time = time.time()
        self._finish_time = None

        self.path, sep, self.query = uri.partition('?')
        self.arguments = parse_qs_bytes(self.query, keep_blank_values=True)

    def supports_http_1_1(self):
        """Returns True if this request supports HTTP/1.1 semantics"""
        return self.version == "HTTP/1.1"

    @property
    def cookies(self):
        """A dictionary of Cookie.Morsel objects."""
        if not hasattr(self, "_cookies"):
            self._cookies = Cookie.SimpleCookie()
            if "Cookie" in self.headers:
                try:
                    self._cookies.load(
                        native_str(self.headers["Cookie"]))
                except Exception:
                    self._cookies = {}
        return self._cookies

    def write(self, chunk, callback=None):
        """Writes the given chunk to the response stream."""
        assert isinstance(chunk, bytes_type)
        # 执行HTTPConnection的write方法
        self.connection.write(chunk, callback=callback)

    def finish(self):
        """Finishes this HTTP request on the open connection."""
        self.connection.finish()
        self._finish_time = time.time()

    def full_url(self):
        """Reconstructs the full URL for this request."""
        return self.protocol + "://" + self.host + self.uri

    def request_time(self):
        """Returns the amount of time it took for this request to execute."""
        if self._finish_time is None:
            return time.time() - self._start_time
        else:
            return self._finish_time - self._start_time

    def get_ssl_certificate(self, binary_form=False):
        """Returns the client's SSL certificate, if any.

        To use client certificates, the HTTPServer must have been constructed
        with cert_reqs set in ssl_options, e.g.::

            server = HTTPServer(app,
                ssl_options=dict(
                    certfile="foo.crt",
                    keyfile="foo.key",
                    cert_reqs=ssl.CERT_REQUIRED,
                    ca_certs="cacert.crt"))

        By default, the return value is a dictionary (or None, if no
        client certificate is present).  If ``binary_form`` is true, a
        DER-encoded form of the certificate is returned instead.  See
        SSLSocket.getpeercert() in the standard library for more
        details.
        http://docs.python.org/library/ssl.html#sslsocket-objects
        """
        try:
            return self.connection.stream.socket.getpeercert(
                binary_form=binary_form)
        except ssl.SSLError:
            return None

    def __repr__(self):
        attrs = ("protocol", "host", "method", "uri", "version", "remote_ip")
        args = ", ".join(["%s=%r" % (n, getattr(self, n)) for n in attrs])
        return "%s(%s, headers=%s)" % (
            self.__class__.__name__, args, dict(self.headers))

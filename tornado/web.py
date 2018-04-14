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

"""``tornado.web`` provides a simple web framework with asynchronous
features that allow it to scale to large numbers of open connections,
making it ideal for `long polling
<http://en.wikipedia.org/wiki/Push_technology#Long_polling>`_.

Here is a simple "Hello, world" example app::

    import tornado.ioloop
    import tornado.web

    class MainHandler(tornado.web.RequestHandler):
        def get(self):
            self.write("Hello, world")

    if __name__ == "__main__":
        application = tornado.web.Application([
            (r"/", MainHandler),
        ])
        application.listen(8888)
        tornado.ioloop.IOLoop.instance().start()

See the :doc:`Tornado overview <overview>` for more details and a good getting
started guide.

Thread-safety notes
-------------------

In general, methods on `RequestHandler` and elsewhere in Tornado are
not thread-safe.  In particular, methods such as
`~RequestHandler.write()`, `~RequestHandler.finish()`, and
`~RequestHandler.flush()` must only be called from the main thread.  If
you use multiple threads it is important to use `.IOLoop.add_callback`
to transfer control back to the main thread before finishing the
request.
"""

from __future__ import absolute_import, division, print_function, with_statement


import base64
import binascii
import datetime
import email.utils
import functools
import gzip
import hashlib
import hmac
import mimetypes
import numbers
import os.path
import re
import stat
import sys
import threading
import time
import tornado
import traceback
import types
import uuid

from tornado.concurrent import Future
from tornado import escape
from tornado import httputil
from tornado import locale
from tornado.log import access_log, app_log, gen_log
from tornado import stack_context
from tornado import template
from tornado.escape import utf8, _unicode
from tornado.util import bytes_type, import_object, ObjectDict, raise_exc_info, unicode_type

try:
    from io import BytesIO  # python 3
except ImportError:
    from cStringIO import StringIO as BytesIO  # python 2

try:
    import Cookie  # py2
except ImportError:
    import http.cookies as Cookie  # py3

try:
    import urlparse  # py2
except ImportError:
    import urllib.parse as urlparse  # py3

try:
    from urllib import urlencode  # py2
except ImportError:
    from urllib.parse import urlencode  # py3


# Tornado的web框架(tornado.web)在web.py中实现，
# 主要包括RequestHandler类（本质为对http请求处理的封装）和Application类
# （是一些列请求处理的集合，构成的一个web-application，
# 源代码注释不翻译更容易理解：A collection of request handlers that make up a web application）。


# 从Application的运行流程可以看出，RequestHandler 类把 _execute 方法【暴露】给了 application 对象，
# 在这个方法里完成了【请求】的【具体分发】和【处理】。
# 因此，我主要看这一方法（当然还包括__init__)，
# 其它方法在开发应用时自然会用到，还是比较实用的：
# 比如header,cookie,get/post参数的getter/setter方法，都是必须的。

class RequestHandler(object):
    """Subclass this class and define `get()` or `post()` to make a handler.

    If you want to support more methods than the standard GET/HEAD/POST, you
    should override the class variable ``SUPPORTED_METHODS`` in your
    `RequestHandler` subclass.
    """
    SUPPORTED_METHODS = ("GET", "HEAD", "POST", "DELETE", "PATCH", "PUT",
                         "OPTIONS")

    _template_loaders = {}  # {path: template.BaseLoader}
    _template_loader_lock = threading.Lock()
    _remove_control_chars_regex = re.compile(r"[\x00-\x08\x0e-\x1f]")

    def __init__(self, application, request, **kwargs):

        # RequestHandler
        # 完成具体的请求，开发者需要继承它，并根据需要，覆盖
        # head 、 get 、 post 、 delete 、 patch 、 put 、 options
        # 等方法，定义处理对应请求的业务逻辑。
        # RequestHandler
        # 提供了很多钩子(自己可以自定义的函数)，包括
        # initialize 、 prepare 、 on_finish 、 on_connection_close 、 set_default_headers


        # __init__。负责对象的初始化，在对象被构造时一定会被调用的。
        # 对象是什么时候被调用的呢？从分析Application中可以看到，
        # 在.*主机的路径路由列表里，当路径正则匹配了当前请求的路由时，
        # application 就会新建一个【 RequestHandler 】对象（实际上是【子类对象】），
        # 然后调用 _execute 方法。
        # __init__ 方法接受3个参数 : application, request, **kwargs，
        # 分别是application单例，当前请求request 和 kwargs
        # （暂时没被用上。不过可以覆盖initialize方法，里面就有它）。
        # 这个kwargs 是静态存储在路由列表里的，它最初是在给 application 设置路由列表时除了路径正则，
        # 处理器类之外的第三个对象，是一个字典，一般情况是空的。
        # __init__ 方法也没做什么，就是建立了这个对象和 application, request 的【关联】就完了。
        # 构造器就应该是这样，对吧？
        # 接下来会调用 _execute 方法。
        super(RequestHandler, self).__init__()

        self.application = application
        self.request = request
        self._headers_written = False
        self._finished = False
        self._auto_finish = True
        self._transforms = None  # will be set in _execute
        self.path_args = None
        self.path_kwargs = None

        # 获取在application中设置的 ui_modules 和ui_method
        self.ui = ObjectDict((n, self._ui_method(m)) for n, m in
                             application.ui_methods.items())
        # UIModules are available as both `modules` and `_tt_modules` in the
        # template namespace.  Historically only `modules` was available
        # but could be clobbered by user additions to the namespace.
        # The template {% module %} directive looks in `_tt_modules` to avoid
        # possible conflicts.
        self.ui["_tt_modules"] = _UIModuleNamespace(self,
                                                    application.ui_modules)
        self.ui["modules"] = self.ui["_tt_modules"]
        self.clear()  #  设置服务器、内容类型编码和连接
        # Check since connection is not available in WSGI
        # 检查连接是否可用，应该是长短连接有关。
        if getattr(self.request, "connection", None):
            self.request.connection.set_close_callback(
                self.on_connection_close)
        self.initialize(**kwargs)

    def initialize(self):
        """Hook for subclass initialization.

        A dictionary passed as the third argument of a url spec will be
        supplied as keyword arguments to initialize().

        Example::

            class ProfileHandler(RequestHandler):
                def initialize(self, database):
                    self.database = database

                def get(self, username):
                    ...

            app = Application([
                (r'/user/(.*)', ProfileHandler, dict(database=database)),
                ])
        """
        pass

    @property
    def settings(self):
        """An alias for `self.application.settings <Application.settings>`."""
        return self.application.settings

    def head(self, *args, **kwargs):
        raise HTTPError(405)

    def get(self, *args, **kwargs):
        raise HTTPError(405)

    def post(self, *args, **kwargs):
        raise HTTPError(405)

    def delete(self, *args, **kwargs):
        raise HTTPError(405)

    def patch(self, *args, **kwargs):
        raise HTTPError(405)

    def put(self, *args, **kwargs):
        raise HTTPError(405)

    def options(self, *args, **kwargs):
        raise HTTPError(405)

    def prepare(self):
        """Called at the beginning of a request before  `get`/`post`/etc.

        Override this method to perform common initialization regardless
        of the request method.

        Asynchronous support: Decorate this method with `.gen.coroutine`
        or `.return_future` to make it asynchronous (the
        `asynchronous` decorator cannot be used on `prepare`).
        If this method returns a `.Future` execution will not proceed
        until the `.Future` is done.

        .. versionadded:: 3.1
           Asynchronous support.
        """
        pass

    def on_finish(self):
        """Called after the end of a request.

        Override this method to perform cleanup, logging, etc.
        This method is a counterpart to `prepare`.  ``on_finish`` may
        not produce any output, as it is called after the response
        has been sent to the client.
        """
        pass

    def on_connection_close(self):
        """Called in async handlers if the client closed the connection.

        Override this to clean up resources associated with
        long-lived connections.  Note that this method is called only if
        the connection was closed during asynchronous processing; if you
        need to do cleanup after every request override `on_finish`
        instead.

        Proxies may keep a connection open for a time (perhaps
        indefinitely) after the client has gone away, so this method
        may not be called promptly after the end user closes their
        connection.
        """
        pass

    def clear(self):
        """Resets all headers and content for this response."""
        self._headers = httputil.HTTPHeaders({
            "Server": "TornadoServer/%s" % tornado.version,
            "Content-Type": "text/html; charset=UTF-8",
            "Date": httputil.format_timestamp(time.time()),
        })
        self.set_default_headers()
        if (not self.request.supports_http_1_1() and
            getattr(self.request, 'connection', None) and
                not self.request.connection.no_keep_alive):
            conn_header = self.request.headers.get("Connection")
            if conn_header and (conn_header.lower() == "keep-alive"):
                self.set_header("Connection", "Keep-Alive")
        self._write_buffer = []
        self._status_code = 200
        self._reason = httputil.responses[200]

    def set_default_headers(self):
        """Override this to set HTTP headers at the beginning of the request.

        For example, this is the place to set a custom ``Server`` header.
        Note that setting such headers in the normal flow of request
        processing may not do what you want, since headers may be reset
        during error handling.
        """
        pass

    def set_status(self, status_code, reason=None):
        """Sets the status code for our response.

        :arg int status_code: Response status code. If ``reason`` is ``None``,
            it must be present in `httplib.responses <http.client.responses>`.
        :arg string reason: Human-readable reason phrase describing the status
            code. If ``None``, it will be filled in from
            `httplib.responses <http.client.responses>`.
        """
        self._status_code = status_code
        if reason is not None:
            self._reason = escape.native_str(reason)
        else:
            try:
                self._reason = httputil.responses[status_code]
            except KeyError:
                raise ValueError("unknown status code %d", status_code)

    def get_status(self):
        """Returns the status code for our response."""
        return self._status_code

    def set_header(self, name, value):
        """Sets the given response header name and value.

        If a datetime is given, we automatically format it according to the
        HTTP specification. If the value is not a string, we convert it to
        a string. All header values are then encoded as UTF-8.
        """
        self._headers[name] = self._convert_header_value(value)

    def add_header(self, name, value):
        """Adds the given response header and value.

        Unlike `set_header`, `add_header` may be called multiple times
        to return multiple values for the same header.
        """
        self._headers.add(name, self._convert_header_value(value))

    def clear_header(self, name):
        """Clears an outgoing header, undoing a previous `set_header` call.

        Note that this method does not apply to multi-valued headers
        set by `add_header`.
        """
        if name in self._headers:
            del self._headers[name]

    _INVALID_HEADER_CHAR_RE = re.compile(br"[\x00-\x1f]")

    def _convert_header_value(self, value):
        if isinstance(value, bytes_type):
            pass
        elif isinstance(value, unicode_type):
            value = value.encode('utf-8')
        elif isinstance(value, numbers.Integral):
            # return immediately since we know the converted value will be safe
            return str(value)
        elif isinstance(value, datetime.datetime):
            return httputil.format_timestamp(value)
        else:
            raise TypeError("Unsupported header value %r" % value)
        # If \n is allowed into the header, it is possible to inject
        # additional headers or split the request. Also cap length to
        # prevent obviously erroneous values.
        if (len(value) > 4000 or
                RequestHandler._INVALID_HEADER_CHAR_RE.search(value)):
            raise ValueError("Unsafe header value %r", value)
        return value

    _ARG_DEFAULT = []

    def get_argument(self, name, default=_ARG_DEFAULT, strip=True):
        """Returns the value of the argument with the given name.

        If default is not provided, the argument is considered to be
        required, and we raise a `MissingArgumentError` if it is missing.

        If the argument appears in the url more than once, we return the
        last value.

        The returned value is always unicode.
        """
        args = self.get_arguments(name, strip=strip)
        if not args:
            if default is self._ARG_DEFAULT:
                raise MissingArgumentError(name)
            return default
        return args[-1]

    def get_arguments(self, name, strip=True):
        """Returns a list of the arguments with the given name.

        If the argument is not present, returns an empty list.

        The returned values are always unicode.
        """

        values = []
        for v in self.request.arguments.get(name, []):
            v = self.decode_argument(v, name=name)
            if isinstance(v, unicode_type):
                # Get rid of any weird control chars (unless decoding gave
                # us bytes, in which case leave it alone)
                v = RequestHandler._remove_control_chars_regex.sub(" ", v)
            if strip:
                v = v.strip()
            values.append(v)
        return values

    def decode_argument(self, value, name=None):
        """Decodes an argument from the request.

        The argument has been percent-decoded and is now a byte string.
        By default, this method decodes the argument as utf-8 and returns
        a unicode string, but this may be overridden in subclasses.

        This method is used as a filter for both `get_argument()` and for
        values extracted from the url and passed to `get()`/`post()`/etc.

        The name of the argument is provided if known, but may be None
        (e.g. for unnamed groups in the url regex).
        """
        return _unicode(value)

    @property
    def cookies(self):
        """An alias for `self.request.cookies <.httpserver.HTTPRequest.cookies>`."""
        return self.request.cookies

    def get_cookie(self, name, default=None):
        """Gets the value of the cookie with the given name, else default."""
        if self.request.cookies is not None and name in self.request.cookies:
            return self.request.cookies[name].value
        return default

    def set_cookie(self, name, value, domain=None, expires=None, path="/",
                   expires_days=None, **kwargs):
        """Sets the given cookie name/value with the given options.

        Additional keyword arguments are set on the Cookie.Morsel
        directly.
        See http://docs.python.org/library/cookie.html#morsel-objects
        for available attributes.
        """
        # The cookie library only accepts type str, in both python 2 and 3
        name = escape.native_str(name)
        value = escape.native_str(value)
        if re.search(r"[\x00-\x20]", name + value):
            # Don't let us accidentally inject bad stuff
            raise ValueError("Invalid cookie %r: %r" % (name, value))
        if not hasattr(self, "_new_cookie"):
            self._new_cookie = Cookie.SimpleCookie()
        if name in self._new_cookie:
            del self._new_cookie[name]
        self._new_cookie[name] = value
        morsel = self._new_cookie[name]
        if domain:
            morsel["domain"] = domain
        if expires_days is not None and not expires:
            expires = datetime.datetime.utcnow() + datetime.timedelta(
                days=expires_days)
        if expires:
            morsel["expires"] = httputil.format_timestamp(expires)
        if path:
            morsel["path"] = path
        for k, v in kwargs.items():
            if k == 'max_age':
                k = 'max-age'
            morsel[k] = v

    def clear_cookie(self, name, path="/", domain=None):
        """Deletes the cookie with the given name."""
        expires = datetime.datetime.utcnow() - datetime.timedelta(days=365)
        self.set_cookie(name, value="", path=path, expires=expires,
                        domain=domain)

    def clear_all_cookies(self):
        """Deletes all the cookies the user sent with this request."""
        for name in self.request.cookies:
            self.clear_cookie(name)

    def set_secure_cookie(self, name, value, expires_days=30, **kwargs):
        """Signs and timestamps a cookie so it cannot be forged.

        You must specify the ``cookie_secret`` setting in your Application
        to use this method. It should be a long, random sequence of bytes
        to be used as the HMAC secret for the signature.

        To read a cookie set with this method, use `get_secure_cookie()`.

        Note that the ``expires_days`` parameter sets the lifetime of the
        cookie in the browser, but is independent of the ``max_age_days``
        parameter to `get_secure_cookie`.

        Secure cookies may contain arbitrary byte values, not just unicode
        strings (unlike regular cookies)
        """
        self.set_cookie(name, self.create_signed_value(name, value),
                        expires_days=expires_days, **kwargs)

    def create_signed_value(self, name, value):
        """Signs and timestamps a string so it cannot be forged.

        Normally used via set_secure_cookie, but provided as a separate
        method for non-cookie uses.  To decode a value not stored
        as a cookie use the optional value argument to get_secure_cookie.
        """
        self.require_setting("cookie_secret", "secure cookies")
        return create_signed_value(self.application.settings["cookie_secret"],
                                   name, value)

    def get_secure_cookie(self, name, value=None, max_age_days=31):
        """Returns the given signed cookie if it validates, or None.

        The decoded cookie value is returned as a byte string (unlike
        `get_cookie`).
        """
        self.require_setting("cookie_secret", "secure cookies")
        if value is None:
            value = self.get_cookie(name)
        return decode_signed_value(self.application.settings["cookie_secret"],
                                   name, value, max_age_days=max_age_days)

    def redirect(self, url, permanent=False, status=None):
        """Sends a redirect to the given (optionally relative) URL.

        If the ``status`` argument is specified, that value is used as the
        HTTP status code; otherwise either 301 (permanent) or 302
        (temporary) is chosen based on the ``permanent`` argument.
        The default is 302 (temporary).
        """
        if self._headers_written:
            raise Exception("Cannot redirect after headers have been written")
        if status is None:
            status = 301 if permanent else 302
        else:
            assert isinstance(status, int) and 300 <= status <= 399
        self.set_status(status)
        self.set_header("Location", urlparse.urljoin(utf8(self.request.uri),
                                                     utf8(url)))
        self.finish()

    def write(self, chunk):
        """Writes the given chunk to the output buffer.

        To write the output to the network, use the flush() method below.

        If the given chunk is a dictionary, we write it as JSON and set
        the Content-Type of the response to be ``application/json``.
        (if you want to send JSON as a different ``Content-Type``, call
        set_header *after* calling write()).

        Note that lists are not converted to JSON because of a potential
        cross-site security vulnerability.  All JSON output should be
        wrapped in a dictionary.  More details at
        http://haacked.com/archive/2008/11/20/anatomy-of-a-subtle-json-vulnerability.aspx
        """
        if self._finished:
            raise RuntimeError("Cannot write() after finish().  May be caused "
                               "by using async operations without the "
                               "@asynchronous decorator.")
        if isinstance(chunk, dict):
            chunk = escape.json_encode(chunk)
            self.set_header("Content-Type", "application/json; charset=UTF-8")
        chunk = utf8(chunk)
        self._write_buffer.append(chunk)

    def render(self, template_name, **kwargs):
        """Renders the template with the given arguments as the response."""
        html = self.render_string(template_name, **kwargs)

        # Insert the additional JS and CSS added by the modules on the page
        js_embed = []
        js_files = []
        css_embed = []
        css_files = []
        html_heads = []
        html_bodies = []
        for module in getattr(self, "_active_modules", {}).values():
            embed_part = module.embedded_javascript()
            if embed_part:
                js_embed.append(utf8(embed_part))
            file_part = module.javascript_files()
            if file_part:
                if isinstance(file_part, (unicode_type, bytes_type)):
                    js_files.append(file_part)
                else:
                    js_files.extend(file_part)
            embed_part = module.embedded_css()
            if embed_part:
                css_embed.append(utf8(embed_part))
            file_part = module.css_files()
            if file_part:
                if isinstance(file_part, (unicode_type, bytes_type)):
                    css_files.append(file_part)
                else:
                    css_files.extend(file_part)
            head_part = module.html_head()
            if head_part:
                html_heads.append(utf8(head_part))
            body_part = module.html_body()
            if body_part:
                html_bodies.append(utf8(body_part))

        def is_absolute(path):
            return any(path.startswith(x) for x in ["/", "http:", "https:"])
        if js_files:
            # Maintain order of JavaScript files given by modules
            paths = []
            unique_paths = set()
            for path in js_files:
                if not is_absolute(path):
                    path = self.static_url(path)
                if path not in unique_paths:
                    paths.append(path)
                    unique_paths.add(path)
            js = ''.join('<script src="' + escape.xhtml_escape(p) +
                         '" type="text/javascript"></script>'
                         for p in paths)
            sloc = html.rindex(b'</body>')
            html = html[:sloc] + utf8(js) + b'\n' + html[sloc:]
        if js_embed:
            js = b'<script type="text/javascript">\n//<![CDATA[\n' + \
                b'\n'.join(js_embed) + b'\n//]]>\n</script>'
            sloc = html.rindex(b'</body>')
            html = html[:sloc] + js + b'\n' + html[sloc:]
        if css_files:
            paths = []
            unique_paths = set()
            for path in css_files:
                if not is_absolute(path):
                    path = self.static_url(path)
                if path not in unique_paths:
                    paths.append(path)
                    unique_paths.add(path)
            css = ''.join('<link href="' + escape.xhtml_escape(p) + '" '
                          'type="text/css" rel="stylesheet"/>'
                          for p in paths)
            hloc = html.index(b'</head>')
            html = html[:hloc] + utf8(css) + b'\n' + html[hloc:]
        if css_embed:
            css = b'<style type="text/css">\n' + b'\n'.join(css_embed) + \
                b'\n</style>'
            hloc = html.index(b'</head>')
            html = html[:hloc] + css + b'\n' + html[hloc:]
        if html_heads:
            hloc = html.index(b'</head>')
            html = html[:hloc] + b''.join(html_heads) + b'\n' + html[hloc:]
        if html_bodies:
            hloc = html.index(b'</body>')
            html = html[:hloc] + b''.join(html_bodies) + b'\n' + html[hloc:]
        self.finish(html)

    def render_string(self, template_name, **kwargs):
        """Generate the given template with the given arguments.

        We return the generated byte string (in utf8). To generate and
        write a template as a response, use render() above.
        """
        # If no template_path is specified, use the path of the calling file
        template_path = self.get_template_path()
        if not template_path:
            frame = sys._getframe(0)
            web_file = frame.f_code.co_filename
            while frame.f_code.co_filename == web_file:
                frame = frame.f_back
            template_path = os.path.dirname(frame.f_code.co_filename)
        with RequestHandler._template_loader_lock:
            if template_path not in RequestHandler._template_loaders:
                loader = self.create_template_loader(template_path)
                RequestHandler._template_loaders[template_path] = loader
            else:
                loader = RequestHandler._template_loaders[template_path]
        t = loader.load(template_name)
        namespace = self.get_template_namespace()
        namespace.update(kwargs)
        return t.generate(**namespace)

    def get_template_namespace(self):
        """Returns a dictionary to be used as the default template namespace.

        May be overridden by subclasses to add or modify values.

        The results of this method will be combined with additional
        defaults in the `tornado.template` module and keyword arguments
        to `render` or `render_string`.
        """
        namespace = dict(
            handler=self,
            request=self.request,
            current_user=self.current_user,
            locale=self.locale,
            _=self.locale.translate,
            static_url=self.static_url,
            xsrf_form_html=self.xsrf_form_html,
            reverse_url=self.reverse_url
        )
        namespace.update(self.ui)
        return namespace

    def create_template_loader(self, template_path):
        """Returns a new template loader for the given path.

        May be overridden by subclasses.  By default returns a
        directory-based loader on the given path, using the
        ``autoescape`` application setting.  If a ``template_loader``
        application setting is supplied, uses that instead.
        """
        settings = self.application.settings
        if "template_loader" in settings:
            return settings["template_loader"]
        kwargs = {}
        if "autoescape" in settings:
            # autoescape=None means "no escaping", so we have to be sure
            # to only pass this kwarg if the user asked for it.
            kwargs["autoescape"] = settings["autoescape"]
        return template.Loader(template_path, **kwargs)

    def flush(self, include_footers=False, callback=None):
        """Flushes the current output buffer to the network.

        The ``callback`` argument, if given, can be used for flow control:
        it will be run when all flushed data has been written to the socket.
        Note that only one flush callback can be outstanding at a time;
        if another flush occurs before the previous flush's callback
        has been run, the previous callback will be discarded.
        """
        # 将处理请求返回的数据发送到IOStream的_write_buffer队列中
        if self.application._wsgi:
            # WSGI applications cannot usefully support flush, so just make
            # it a no-op (and run the callback immediately).
            if callback is not None:
                callback()
            return
        #  取出self._write_buffer 中数据进行组装
        chunk = b"".join(self._write_buffer)
        # 取出数据后重置_write_buffer列表
        self._write_buffer = []
        if not self._headers_written:
            self._headers_written = True
            for transform in self._transforms:
                self._status_code, self._headers, chunk = \
                    transform.transform_first_chunk(
                        self._status_code, self._headers, chunk, include_footers)
            headers = self._generate_headers()
        else:
            for transform in self._transforms:
                chunk = transform.transform_chunk(chunk, include_footers)
            headers = b""

        # Ignore the chunk and only write the headers for HEAD requests
        if self.request.method == "HEAD":
            if headers:
                self.request.write(headers, callback=callback)
            return
        # #执行HTTPReqeust的write方法
        self.request.write(headers + chunk, callback=callback)

    def finish(self, chunk=None):
        """Finishes this response, ending the HTTP request."""

        # 还有一个 finish 方法，它在业务逻辑代码【后】被执行。【缓冲区里的内容】要被输出，连接总得被【关闭】，
        # 资源总得被【释放】，所以这些【善后事宜】就交给了 finish。
        # 与缓冲区相关的还有一个函数 flush，
        # 顾名思义它会调用 transforms 对输出做预处理，然后拼接缓冲区一次性输出
        # self.request.write(headers + chunk, callback=callback)

        # 以上，就是handler的分析。handler 的读与写，实际上都是依靠request对象来完成的，而
        # request 到底如何呢？
        if self._finished:
            raise RuntimeError("finish() called twice.  May be caused "
                               "by using async operations without the "
                               "@asynchronous decorator.")

        if chunk is not None:
            # 把数据写入到self._write_buffer中，等待处理请求返回的数据发送到IOStream的_write_buffer队列中
            self.write(chunk)

        # Automatically support ETags and add the Content-Length header if
        # we have not flushed any content yet.
        if not self._headers_written:
            if (self._status_code == 200 and
                self.request.method in ("GET", "HEAD") and
                    "Etag" not in self._headers):
                self.set_etag_header()
                if self.check_etag_header():
                    self._write_buffer = []
                    self.set_status(304)
            if self._status_code == 304:
                assert not self._write_buffer, "Cannot send body with 304"
                self._clear_headers_for_304()
            elif "Content-Length" not in self._headers:
                content_length = sum(len(part) for part in self._write_buffer)
                self.set_header("Content-Length", content_length)

        if hasattr(self.request, "connection"):
            # Now that the request is finished, clear the callback we
            # set on the HTTPConnection (which would otherwise prevent the
            # garbage collection of the RequestHandler when there
            # are keepalive connections)
            self.request.connection.set_close_callback(None)

        if not self.application._wsgi:
            # 将处理请求返回的数据发送到IOStream的_write_buffer队列中
            self.flush(include_footers=True)
            self.request.finish()
            self._log()
        self._finished = True
        self.on_finish()
        # Break up a reference cycle between this handler and the
        # _ui_module closures to allow for faster GC on CPython.
        self.ui = None

    def send_error(self, status_code=500, **kwargs):
        """Sends the given HTTP error code to the browser.

        If `flush()` has already been called, it is not possible to send
        an error, so this method will simply terminate the response.
        If output has been written but not yet flushed, it will be discarded
        and replaced with the error page.

        Override `write_error()` to customize the error page that is returned.
        Additional keyword arguments are passed through to `write_error`.
        """
        if self._headers_written:
            gen_log.error("Cannot send error response after headers written")
            if not self._finished:
                self.finish()
            return
        self.clear()

        reason = None
        if 'exc_info' in kwargs:
            exception = kwargs['exc_info'][1]
            if isinstance(exception, HTTPError) and exception.reason:
                reason = exception.reason
        self.set_status(status_code, reason=reason)
        try:
            self.write_error(status_code, **kwargs)
        except Exception:
            app_log.error("Uncaught exception in write_error", exc_info=True)
        if not self._finished:
            self.finish()

    def write_error(self, status_code, **kwargs):
        """Override to implement custom error pages.

        ``write_error`` may call `write`, `render`, `set_header`, etc
        to produce output as usual.

        If this error was caused by an uncaught exception (including
        HTTPError), an ``exc_info`` triple will be available as
        ``kwargs["exc_info"]``.  Note that this exception may not be
        the "current" exception for purposes of methods like
        ``sys.exc_info()`` or ``traceback.format_exc``.

        For historical reasons, if a method ``get_error_html`` exists,
        it will be used instead of the default ``write_error`` implementation.
        ``get_error_html`` returned a string instead of producing output
        normally, and had different semantics for exception handling.
        Users of ``get_error_html`` are encouraged to convert their code
        to override ``write_error`` instead.
        """
        if hasattr(self, 'get_error_html'):
            if 'exc_info' in kwargs:
                exc_info = kwargs.pop('exc_info')
                kwargs['exception'] = exc_info[1]
                try:
                    # Put the traceback into sys.exc_info()
                    raise_exc_info(exc_info)
                except Exception:
                    self.finish(self.get_error_html(status_code, **kwargs))
            else:
                self.finish(self.get_error_html(status_code, **kwargs))
            return
        if self.settings.get("debug") and "exc_info" in kwargs:
            # in debug mode, try to send a traceback
            self.set_header('Content-Type', 'text/plain')
            for line in traceback.format_exception(*kwargs["exc_info"]):
                self.write(line)
            self.finish()
        else:
            self.finish("<html><title>%(code)d: %(message)s</title>"
                        "<body>%(code)d: %(message)s</body></html>" % {
                            "code": status_code,
                            "message": self._reason,
                        })

    @property
    def locale(self):
        """The local for the current session.

        Determined by either `get_user_locale`, which you can override to
        set the locale based on, e.g., a user preference stored in a
        database, or `get_browser_locale`, which uses the ``Accept-Language``
        header.
        """
        if not hasattr(self, "_locale"):
            self._locale = self.get_user_locale()
            if not self._locale:
                self._locale = self.get_browser_locale()
                assert self._locale
        return self._locale

    def get_user_locale(self):
        """Override to determine the locale from the authenticated user.

        If None is returned, we fall back to `get_browser_locale()`.

        This method should return a `tornado.locale.Locale` object,
        most likely obtained via a call like ``tornado.locale.get("en")``
        """
        return None

    def get_browser_locale(self, default="en_US"):
        """Determines the user's locale from ``Accept-Language`` header.

        See http://www.w3.org/Protocols/rfc2616/rfc2616-sec14.html#sec14.4
        """
        if "Accept-Language" in self.request.headers:
            languages = self.request.headers["Accept-Language"].split(",")
            locales = []
            for language in languages:
                parts = language.strip().split(";")
                if len(parts) > 1 and parts[1].startswith("q="):
                    try:
                        score = float(parts[1][2:])
                    except (ValueError, TypeError):
                        score = 0.0
                else:
                    score = 1.0
                locales.append((parts[0], score))
            if locales:
                locales.sort(key=lambda pair: pair[1], reverse=True)
                codes = [l[0] for l in locales]
                return locale.get(*codes)
        return locale.get(default)

    @property
    def current_user(self):
        """The authenticated user for this request.

        This is a cached version of `get_current_user`, which you can
        override to set the user based on, e.g., a cookie. If that
        method is not overridden, this method always returns None.

        We lazy-load the current user the first time this method is called
        and cache the result after that.
        """
        if not hasattr(self, "_current_user"):
            self._current_user = self.get_current_user()
        return self._current_user

    @current_user.setter
    def current_user(self, value):
        self._current_user = value

    def get_current_user(self):
        """Override to determine the current user from, e.g., a cookie."""
        return None

    def get_login_url(self):
        """Override to customize the login URL based on the request.

        By default, we use the ``login_url`` application setting.
        """
        self.require_setting("login_url", "@tornado.web.authenticated")
        return self.application.settings["login_url"]

    def get_template_path(self):
        """Override to customize template path for each handler.

        By default, we use the ``template_path`` application setting.
        Return None to load templates relative to the calling file.
        """
        return self.application.settings.get("template_path")

    @property
    def xsrf_token(self):
        """The XSRF-prevention token for the current user/session.

        To prevent cross-site request forgery, we set an '_xsrf' cookie
        and include the same '_xsrf' value as an argument with all POST
        requests. If the two do not match, we reject the form submission
        as a potential forgery.

        See http://en.wikipedia.org/wiki/Cross-site_request_forgery
        """
        if not hasattr(self, "_xsrf_token"):
            token = self.get_cookie("_xsrf")
            if not token:
                token = binascii.b2a_hex(uuid.uuid4().bytes)
                expires_days = 30 if self.current_user else None
                self.set_cookie("_xsrf", token, expires_days=expires_days)
            self._xsrf_token = token
        return self._xsrf_token

    def check_xsrf_cookie(self):
        """Verifies that the ``_xsrf`` cookie matches the ``_xsrf`` argument.

        To prevent cross-site request forgery, we set an ``_xsrf``
        cookie and include the same value as a non-cookie
        field with all ``POST`` requests. If the two do not match, we
        reject the form submission as a potential forgery.

        The ``_xsrf`` value may be set as either a form field named ``_xsrf``
        or in a custom HTTP header named ``X-XSRFToken`` or ``X-CSRFToken``
        (the latter is accepted for compatibility with Django).

        See http://en.wikipedia.org/wiki/Cross-site_request_forgery

        Prior to release 1.1.1, this check was ignored if the HTTP header
        ``X-Requested-With: XMLHTTPRequest`` was present.  This exception
        has been shown to be insecure and has been removed.  For more
        information please see
        http://www.djangoproject.com/weblog/2011/feb/08/security/
        http://weblog.rubyonrails.org/2011/2/8/csrf-protection-bypass-in-ruby-on-rails
        """
        token = (self.get_argument("_xsrf", None) or
                 self.request.headers.get("X-Xsrftoken") or
                 self.request.headers.get("X-Csrftoken"))
        if not token:
            raise HTTPError(403, "'_xsrf' argument missing from POST")
        if self.xsrf_token != token:
            raise HTTPError(403, "XSRF cookie does not match POST argument")

    def xsrf_form_html(self):
        """An HTML ``<input/>`` element to be included with all POST forms.

        It defines the ``_xsrf`` input value, which we check on all POST
        requests to prevent cross-site request forgery. If you have set
        the ``xsrf_cookies`` application setting, you must include this
        HTML within all of your HTML forms.

        In a template, this method should be called with ``{% module
        xsrf_form_html() %}``

        See `check_xsrf_cookie()` above for more information.
        """
        return '<input type="hidden" name="_xsrf" value="' + \
            escape.xhtml_escape(self.xsrf_token) + '"/>'

    def static_url(self, path, include_host=None, **kwargs):
        """Returns a static URL for the given relative static file path.

        This method requires you set the ``static_path`` setting in your
        application (which specifies the root directory of your static
        files).

        This method returns a versioned url (by default appending
        ``?v=<signature>``), which allows the static files to be
        cached indefinitely.  This can be disabled by passing
        ``include_version=False`` (in the default implementation;
        other static file implementations are not required to support
        this, but they may support other options).

        By default this method returns URLs relative to the current
        host, but if ``include_host`` is true the URL returned will be
        absolute.  If this handler has an ``include_host`` attribute,
        that value will be used as the default for all `static_url`
        calls that do not pass ``include_host`` as a keyword argument.

        """
        self.require_setting("static_path", "static_url")
        get_url = self.settings.get("static_handler_class",
                                    StaticFileHandler).make_static_url

        if include_host is None:
            include_host = getattr(self, "include_host", False)

        if include_host:
            base = self.request.protocol + "://" + self.request.host
        else:
            base = ""

        return base + get_url(self.settings, path, **kwargs)

    def async_callback(self, callback, *args, **kwargs):
        """Obsolete - catches exceptions from the wrapped function.

        This function is unnecessary since Tornado 1.1.
        """
        if callback is None:
            return None
        if args or kwargs:
            callback = functools.partial(callback, *args, **kwargs)

        def wrapper(*args, **kwargs):
            try:
                return callback(*args, **kwargs)
            except Exception as e:
                if self._headers_written:
                    app_log.error("Exception after headers written",
                                  exc_info=True)
                else:
                    self._handle_request_exception(e)
        return wrapper

    def require_setting(self, name, feature="this feature"):
        """Raises an exception if the given app setting is not defined."""
        if not self.application.settings.get(name):
            raise Exception("You must define the '%s' setting in your "
                            "application to use %s" % (name, feature))

    def reverse_url(self, name, *args):
        """Alias for `Application.reverse_url`."""
        return self.application.reverse_url(name, *args)

    def compute_etag(self):
        """Computes the etag header to be used for this request.

        By default uses a hash of the content written so far.

        May be overridden to provide custom etag implementations,
        or may return None to disable tornado's default etag support.
        """
        hasher = hashlib.sha1()
        for part in self._write_buffer:
            hasher.update(part)
        return '"%s"' % hasher.hexdigest()

    def set_etag_header(self):
        """Sets the response's Etag header using ``self.compute_etag()``.

        Note: no header will be set if ``compute_etag()`` returns ``None``.

        This method is called automatically when the request is finished.
        """
        etag = self.compute_etag()
        if etag is not None:
            self.set_header("Etag", etag)

    def check_etag_header(self):
        """Checks the ``Etag`` header against requests's ``If-None-Match``.

        Returns ``True`` if the request's Etag matches and a 304 should be
        returned. For example::

            self.set_etag_header()
            if self.check_etag_header():
                self.set_status(304)
                return

        This method is called automatically when the request is finished,
        but may be called earlier for applications that override
        `compute_etag` and want to do an early check for ``If-None-Match``
        before completing the request.  The ``Etag`` header should be set
        (perhaps with `set_etag_header`) before calling this method.
        """
        etag = self._headers.get("Etag")
        inm = utf8(self.request.headers.get("If-None-Match", ""))
        return bool(etag and inm and inm.find(etag) >= 0)

    def _stack_context_handle_exception(self, type, value, traceback):
        try:
            # For historical reasons _handle_request_exception only takes
            # the exception value instead of the full triple,
            # so re-raise the exception to ensure that it's in
            # sys.exc_info()
            raise_exc_info((type, value, traceback))
        except Exception:
            self._handle_request_exception(value)
        return True

    def _execute(self, transforms, *args, **kwargs):
        """Executes this request with the given output transforms."""


        # 该方法接受三个参数
        # 1.transforms(相当于 application 的中间件吧，对流程没有影响），
        # 2.*args(用数字做索引时的正则 group)，
        # 3.**kwargs(用字符串做键值时的正则 group，与__init__的类似但却是【动态】的)。
        # 该方法
        # 先设定好 transform（因为不管错误与否都算输出，因此中间件都必须到位）。
        # 然后，检查 http 方法是否被支持，
        # 然后整合路径里的参数，检查 XSRF 攻击。
        # 然后 prepare()。这总是在【业务逻辑【前】】被执行，
        # preapare()函数是自定义函数
        # 在【业务逻辑【后】】还有个 finish()。
        # 业务逻辑代码被定义在子类的 get/post/put/delete 方法里，具体视详细情况而定。

        # 处理请求
        self._transforms = transforms
        try:
            if self.request.method not in self.SUPPORTED_METHODS:
                raise HTTPError(405)
            self.path_args = [self.decode_argument(arg) for arg in args]
            self.path_kwargs = dict((k, self.decode_argument(v, name=k))
                                    for (k, v) in kwargs.items())
            # If XSRF cookies are turned on, reject form submissions without
            # the proper cookie
            if self.request.method not in ("GET", "HEAD", "OPTIONS") and \
                    self.application.settings.get("xsrf_cookies"):
                self.check_xsrf_cookie()
            self._when_complete(self.prepare(), self._execute_method)
        except Exception as e:
            self._handle_request_exception(e)

    def _when_complete(self, result, callback):
        try:
            if result is None:
                callback()
            elif isinstance(result, Future):
                if result.done():
                    if result.result() is not None:
                        raise ValueError('Expected None, got %r' % result)
                    callback()
                else:
                    # Delayed import of IOLoop because it's not available
                    # on app engine
                    from tornado.ioloop import IOLoop
                    IOLoop.current().add_future(
                        result, functools.partial(self._when_complete,
                                                  callback=callback))
            else:
                raise ValueError("Expected Future or None, got %r" % result)
        except Exception as e:
            self._handle_request_exception(e)

    def _execute_method(self):
        # 通过反射的方法，执行 RequestHandler 派生类的的 get、post、put方法
        if not self._finished:
            method = getattr(self, self.request.method.lower())
            self._when_complete(method(*self.path_args, **self.path_kwargs),
                                self._execute_finish)

    def _execute_finish(self):
        if self._auto_finish and not self._finished:
            self.finish()

    def _generate_headers(self):
        reason = self._reason
        lines = [utf8(self.request.version + " " +
                      str(self._status_code) +
                      " " + reason)]
        lines.extend([utf8(n) + b": " + utf8(v) for n, v in self._headers.get_all()])

        if hasattr(self, "_new_cookie"):
            for cookie in self._new_cookie.values():
                lines.append(utf8("Set-Cookie: " + cookie.OutputString(None)))
        return b"\r\n".join(lines) + b"\r\n\r\n"

    def _log(self):
        """Logs the current request.

        Sort of deprecated since this functionality was moved to the
        Application, but left in place for the benefit of existing apps
        that have overridden this method.
        """
        self.application.log_request(self)

    def _request_summary(self):
        return self.request.method + " " + self.request.uri + \
            " (" + self.request.remote_ip + ")"

    def _handle_request_exception(self, e):
        self.log_exception(*sys.exc_info())
        if self._finished:
            # Extra errors after the request has been finished should
            # be logged, but there is no reason to continue to try and
            # send a response.
            return
        if isinstance(e, HTTPError):
            if e.status_code not in httputil.responses and not e.reason:
                gen_log.error("Bad HTTP status code: %d", e.status_code)
                self.send_error(500, exc_info=sys.exc_info())
            else:
                self.send_error(e.status_code, exc_info=sys.exc_info())
        else:
            self.send_error(500, exc_info=sys.exc_info())

    def log_exception(self, typ, value, tb):
        """Override to customize logging of uncaught exceptions.

        By default logs instances of `HTTPError` as warnings without
        stack traces (on the ``tornado.general`` logger), and all
        other exceptions as errors with stack traces (on the
        ``tornado.application`` logger).

        .. versionadded:: 3.1
        """
        if isinstance(value, HTTPError):
            if value.log_message:
                format = "%d %s: " + value.log_message
                args = ([value.status_code, self._request_summary()] +
                        list(value.args))
                gen_log.warning(format, *args)
        else:
            app_log.error("Uncaught exception %s\n%r", self._request_summary(),
                          self.request, exc_info=(typ, value, tb))

    def _ui_module(self, name, module):
        def render(*args, **kwargs):
            if not hasattr(self, "_active_modules"):
                self._active_modules = {}
            if name not in self._active_modules:
                self._active_modules[name] = module(self)
            rendered = self._active_modules[name].render(*args, **kwargs)
            return rendered
        return render

    def _ui_method(self, method):
        return lambda *args, **kwargs: method(self, *args, **kwargs)

    def _clear_headers_for_304(self):
        # 304 responses should not contain entity headers (defined in
        # http://www.w3.org/Protocols/rfc2616/rfc2616-sec7.html#sec7.1)
        # not explicitly allowed by
        # http://www.w3.org/Protocols/rfc2616/rfc2616-sec10.html#sec10.3.5
        headers = ["Allow", "Content-Encoding", "Content-Language",
                   "Content-Length", "Content-MD5", "Content-Range",
                   "Content-Type", "Last-Modified"]
        for h in headers:
            self.clear_header(h)


def asynchronous(method):
    """Wrap request handler methods with this if they are asynchronous.

    This decorator is unnecessary if the method is also decorated with
    ``@gen.coroutine`` (it is legal but unnecessary to use the two
    decorators together, in which case ``@asynchronous`` must be
    first).

    This decorator should only be applied to the :ref:`HTTP verb
    methods <verbs>`; its behavior is undefined for any other method.
    This decorator does not *make* a method asynchronous; it tells
    the framework that the method *is* asynchronous.  For this decorator
    to be useful the method must (at least sometimes) do something
    asynchronous.

    If this decorator is given, the response is not finished when the
    method returns. It is up to the request handler to call
    `self.finish() <RequestHandler.finish>` to finish the HTTP
    request. Without this decorator, the request is automatically
    finished when the ``get()`` or ``post()`` method returns. Example::

       class MyRequestHandler(web.RequestHandler):
           @web.asynchronous
           def get(self):
              http = httpclient.AsyncHTTPClient()
              http.fetch("http://friendfeed.com/", self._on_download)

           def _on_download(self, response):
              self.write("Downloaded!")
              self.finish()

    .. versionadded:: 3.1
       The ability to use ``@gen.coroutine`` without ``@asynchronous``.
    """
    # Delay the IOLoop import because it's not available on app engine.

    # asynchronous方法
    # 该方法通常被用为:[请求处理函数的decorator]，以实现异步操作，被@asynchronous修饰后的请求处理为
    # 【长连接】，在调用self.finish之前会一直处于【连接等待状态】。


    from tornado.ioloop import IOLoop
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        if self.application._wsgi:
            raise Exception("@asynchronous is not supported for WSGI apps")
        self._auto_finish = False
        with stack_context.ExceptionStackContext(
                self._stack_context_handle_exception):
            result = method(self, *args, **kwargs)
            if isinstance(result, Future):
                # If @asynchronous is used with @gen.coroutine, (but
                # not @gen.engine), we can automatically finish the
                # request when the future resolves.  Additionally,
                # the Future will swallow any exceptions so we need
                # to throw them back out to the stack context to finish
                # the request.
                def future_complete(f):
                    f.result()
                    if not self._finished:
                        self.finish()
                IOLoop.current().add_future(result, future_complete)
            return result
    return wrapper


def removeslash(method):
    """Use this decorator to remove trailing slashes from the request path.

    For example, a request to ``/foo/`` would redirect to ``/foo`` with this
    decorator. Your request handler mapping should use a regular expression
    like ``r'/foo/*'`` in conjunction with using the decorator.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        if self.request.path.endswith("/"):
            if self.request.method in ("GET", "HEAD"):
                uri = self.request.path.rstrip("/")
                if uri:  # don't try to redirect '/' to ''
                    if self.request.query:
                        uri += "?" + self.request.query
                    self.redirect(uri, permanent=True)
                    return
            else:
                raise HTTPError(404)
        return method(self, *args, **kwargs)
    return wrapper


def addslash(method):
    """Use this decorator to add a missing trailing slash to the request path.

    For example, a request to ``/foo`` would redirect to ``/foo/`` with this
    decorator. Your request handler mapping should use a regular expression
    like ``r'/foo/?'`` in conjunction with using the decorator.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        if not self.request.path.endswith("/"):
            if self.request.method in ("GET", "HEAD"):
                uri = self.request.path + "/"
                if self.request.query:
                    uri += "?" + self.request.query
                self.redirect(uri, permanent=True)
                return
            raise HTTPError(404)
        return method(self, *args, **kwargs)
    return wrapper

# 对Application 的总结
# Application 的__init__方法设定了.*主机的路径路由规则，
# listen 方法里开启了服务器并把自身作为回调。
# __call__方法在服务器 accept 到一个新连接时被调用，
# 主要是根据路由规则转发请求到不同的处理器类，并在处理器里被分派到对应的具体方法中，到此完成请求的处理。

class Application(object):
    """A collection of request handlers that make up a web application.

    Instances of this class are callable and can be passed directly to
    HTTPServer to serve the application::

        application = web.Application([
            (r"/", MainPageHandler),
        ])
        http_server = httpserver.HTTPServer(application)
        http_server.listen(8080)
        ioloop.IOLoop.instance().start()

    The constructor for this class takes in a list of `URLSpec` objects
    or (regexp, request_class) tuples. When we receive requests, we
    iterate over the list in order and instantiate an instance of the
    first request class whose regexp matches the request path.

    Each tuple can contain an optional third element, which should be
    a dictionary if it is present. That dictionary is passed as
    keyword arguments to the contructor of the handler. This pattern
    is used for the `StaticFileHandler` in this example (note that a
    `StaticFileHandler` can be installed automatically with the
    static_path setting described below)::

        application = web.Application([
            (r"/static/(.*)", web.StaticFileHandler, {"path": "/var/www"}),
        ])

    We support virtual hosts with the `add_handlers` method, which takes in
    a host regular expression as the first argument::

        application.add_handlers(r"www\.myhost\.com", [
            (r"/article/([0-9]+)", ArticleHandler),
        ])

    You can serve static files by sending the ``static_path`` setting
    as a keyword argument. We will serve those files from the
    ``/static/`` URI (this is configurable with the
    ``static_url_prefix`` setting), and we will serve ``/favicon.ico``
    and ``/robots.txt`` from the same directory.  A custom subclass of
    `StaticFileHandler` can be specified with the
    ``static_handler_class`` setting.
    """

    # 注意：Handler泛指继承自RequestHandler的所有类
    # Handlers泛指继承自RequestHandler的所有类的集合
    def __init__(self, handlers=None, default_host="", transforms=None,
                 wsgi=False, **settings):

        # 初始化Application时，
        # 一般将处理器直接传入，它会调用add_handlers
        # 添加这些处理器，初始化还包括
        # transforms （分块、压缩等）、UI模块、静态文件处理器的初始化。
        # add_handlers 方法负责添加URI和处理器的【映射】


        # 最后看一下__init__方法的流程：
        # 它一般接受一个参数，handlers，亦即最终匹配【主机的路径路】由列表。
        # 1.先设定 transform 列表，
        # 2.再设定静态文件的路由，
        # 3.然后添加主机(.*)的路由列表。

        #
        # 设置响应的编码和返回方式，对应的http相应头：Content-Encoding和Transfer-Encoding
        # Content-Encoding:gzip 表示对数据进行压缩，然后再返回给用户，从而减少流量的传输。
        # Transfer-Encoding:chunck 表示数据的传送方式通过一块一块的传输
        if transforms is None:
            self.transforms = []
            if settings.get("gzip"):
                self.transforms.append(GZipContentEncoding)
            self.transforms.append(ChunkedTransferEncoding)
        else:
            self.transforms = transforms

        # 将参数赋值为【类的变量】（主机名路由列表，它的每一个元素都是一个tuple,包含主机名和对应的路径路由列表）
        self.handlers = []
        self.named_handlers = {}
        self.default_host = default_host
        self.settings = settings

        # ui_modules和ui_methods用于在模版语言中扩展【自定义输出】
        # 这里将tornado内置的ui_modules和ui_methods添加到类的成员变量self.ui_modules
        # 和self.ui_methods中
        self.ui_modules = {'linkify': _linkify,
                           'xsrf_form_html': _xsrf_form_html,
                           'Template': TemplateModule,
                           }
        self.ui_methods = {}
        self._wsgi = wsgi
        # 获取获取用户自定义的ui_modules和ui_methods，
        # 并将他们添加到之前创建的成员变量self.ui_modules和self.ui_methods中
        self._load_ui_modules(settings.get("ui_modules", {}))
        self._load_ui_methods(settings.get("ui_methods", {}))

        # 设置静态文件路径，设置方式则是通过【正则表达式匹配url】，默认让StaticFileHandler来处理匹配的url
        if self.settings.get("static_path"):
            # 从settings中读取key为static_path的值，用于设置静态文件路径
            path = self.settings["static_path"]
            # 获取参数中传入的handlers，如果空则设置为空列表
            handlers = list(handlers or [])
            # 静态文件前缀，默认是/static/
            static_url_prefix = settings.get("static_url_prefix",
                                             "/static/")
            static_handler_class = settings.get("static_handler_class",
                                                StaticFileHandler)
            # 默认处理参数为空
            static_handler_args = settings.get("static_handler_args", {})
            # 覆盖静态路径
            static_handler_args['path'] = path
            #
            # 在参数中传入的handlers前再添加三个映射：
            # 【/static/.*】            -->  StaticFileHandler
            # 【/(favicon\.ico)】    -->  StaticFileHandler
            # 【/(robots\.txt)】        -->  StaticFileHandler
            # handlers = [
            #                (re.escape(static_url_prefix) + r"(.*)", StaticFileHandler, dict(path=path)),
            #                (r"/(favicon\.ico)", StaticFileHandler, dict(path=path)),
            #                (r"/(robots\.txt)", StaticFileHandler, dict(path=path)),
            #            ] + handlers
            for pattern in [re.escape(static_url_prefix) + r"(.*)",
                            r"/(favicon\.ico)", r"/(robots\.txt)"]:

                # 往列表中添加元素
                handlers.insert(0, (pattern, static_handler_class,
                                    static_handler_args))

        # 执行本类的Application的add_handlers方法
        # 此时，handlers是一个列表，其中的每个元素都是一个【对应关系】如上注释部分，
        # 即：url正则表达式和处理匹配该正则的url的Handler
        if handlers:
            self.add_handlers(".*$", handlers)

        # Automatically reload modified modules
        # 如果settings中设置了 debug 模式，那么就使用【自动加载重启】
        if self.settings.get("debug") and not wsgi:
            from tornado import autoreload
            autoreload.start()

    # ”在给定端口上为该应用程序启动HTTP服务器。
    def listen(self, port, address="", **kwargs):
        """Starts an HTTP server for this application on the given port.

        This is a convenience alias for creating an `.HTTPServer`
        object and calling its listen method.  Keyword arguments not
        supported by `HTTPServer.listen <.TCPServer.listen>` are passed to the
        `.HTTPServer` constructor.  For advanced uses
        (e.g. multi-process mode), do not use this method; create an
        `.HTTPServer` and call its
        `.TCPServer.bind`/`.TCPServer.start` methods directly.

        Note that after calling this method you still need to call
        ``IOLoop.instance().start()`` to start the server.
        """
        # import is here rather than top level because HTTPServer
        # is not importable on appengine
        # 创建【服务端】socket，并绑定IP和端口并添加相应设置，
        # 注：【未开始】通过while监听accept，等待客户端连接
        from tornado.httpserver import HTTPServer
        server = HTTPServer(self, **kwargs)
        # 实质是在调用tcpserver的listen方法
        server.listen(port, address)

    def add_handlers(self, host_pattern, host_handlers):
        """Appends the given handlers to our handler list.

        Host patterns are processed sequentially in the order they were
        added. All matching patterns will be considered.
        """

        # add_handlers 方法负责【添加URI】和【处理器的映射】
        # 一直有一个疑问，路由规则是什么时候建立起来的？可以先看一下 add_handlers方法：此方法结构两个参数；
        # 一个是:主机名正则，
        # 一个是：路径路由列表；同时也注意到self.handlers也是一个列表，是主机名路由列表，
        # 它的每一个元素都是一个tuple,包含主机名和对应的路径路由列表。
        #
        # 如果主机模型最后没有结尾符，那么就为他添加一个结尾符。
        if not host_pattern.endswith("$"):
            host_pattern += "$"
        handlers = []
        # The handlers with the wildcard（通配符） host_pattern are a special
        # case - they're added in the constructor（构造函数）but should have lower
        # precedence（优先） than the more-precise（更精确的）handlers（处理程序）added later.
        # If a wildcard handler group exists, it should always be last
        # in the list, so insert new groups just before it.

        # 个人理解是末尾标志位 .*$
        # add_handlers 的流程就很简单了：将路径路由列表和主机名合成一个【tuple】添加到 self.handlers 里，
        # 这样该主机名就会在_get_host_handler 里被检索，就可以【根据】主机名找到对应的路径路由规则了。
        # 这里需要注意的一个问题是：由于.*的特殊性（它会匹配任意字符），
        # 因此总是需要保证它被放置在列表的【最后】，所以就有了这段代码：

        # 之前提到过，默认情况下，所有的主机名都会被匹配，
        # 那是因为在__init__方法里，它调用了 add_handlers(".*",handlers)。
        # 由于.*匹配所有主机名，所以构造 application 对象时传入的路径路由规则列表就是【最终默认路由】列表了。

        #
        # 对主机名先做一层路由映射，例如：http://www.haishiniu.com 和 http://safe.haihsiniu.com
        # 即：safe对应一组url映射，www对应一组url映射，那么当请求到来时，先根据它做第一层匹配，
        # 之后再继续进入内部匹配。

        # 对于第一层url映射来说，由于.*会匹配所有的url，所将 .* 的永远放在handlers列表的最后，
        # 不然 .* 就会截胡了...（也就是直接在前面，起不到任何的过滤作用；）
        # re.complie是【编译正则表达式】，以后请求来的时候只需要执行编译结果的match方法就可以去匹配了

        # 第一层的映射
        ##############################################
        if self.handlers and self.handlers[-1][0].pattern == '.*$':
            self.handlers.insert(-1, (re.compile(host_pattern), handlers))
        else:
            self.handlers.append((re.compile(host_pattern), handlers))

        ###################################################
        # 遍历我们设置的和构造函数中添加的【url->Handler】映射，
        # 将url和对应的Handler封装到URLSpec类中(构造函数中会对url进行编译)
        # 并将所有的URLSpec对象添加到handlers列表中，而handlers列表和主机名模型组成一个元祖，
        # 添加到self.Handlers列表中。
        for spec in host_handlers:
            if isinstance(spec, (tuple, list)):
                assert len(spec) in (2, 3)
                pattern = spec[0]  # 路由规则
                handler = spec[1]  # 处理程序

                # 检查处理程序是函数名(类名)还是说是字符串类型
                if isinstance(handler, str):
                    # import the Module and instantiate the class
                    # Must be a fully qualified（完全合格的） name (module.ClassName)
                    # 字符串转化成可以调用的函数名称
                    handler = import_object(handler)

                # 获取传入的多余参数也就是__init__中的**settings
                if len(spec) == 3:
                    kwargs = spec[2]
                else:
                    kwargs = {}

                # 指定url和处理程序之间的映射
                spec = URLSpec(pattern, handler, kwargs)
            # 类似于：Given the url pattern /([0-9]{4})/([a-z-]+)/, this method
            # would return ('/%s/%s/', 2).

            # 这个handlers作用是啥？
            handlers.append(spec)

            if spec.name:
                # 未使用该功能，默认spec.name = None
                if spec.name in self.named_handlers:
                    app_log.warning(
                        "Multiple handlers named %s; replacing previous value",
                        spec.name)
                self.named_handlers[spec.name] = spec

    def add_transform(self, transform_class):
        self.transforms.append(transform_class)

    def _get_host_handlers(self, request):

        # 取得请求（request）的host(计算机主机)的一组处理器
        # 得到该 host 对应的路径路由列表，默认情况下，任何 host 都会被匹配
        # （原因详见__init__)，
        # 返回的【列表】直接就是传递给构造 application 时的那个 【tuple】 列表

        # 将请求的host和handlers中的主机模型进行匹配
        host = request.host.lower().split(':')[0]
        matches = []
        for pattern, handlers in self.handlers:
            if pattern.match(host):
                matches.extend(handlers)

        # Look for default host if not behind load balancer (for debugging)
        # 在debugging 也就是断掉调试的时候
        if not matches and "X-Real-Ip" not in request.headers:
            for pattern, handlers in self.handlers:
                if pattern.match(self.default_host):
                    matches.extend(handlers)
        return matches or None

    def _load_ui_methods(self, methods):
        if isinstance(methods, types.ModuleType):
            self._load_ui_methods(dict((n, getattr(methods, n))
                                       for n in dir(methods)))
        elif isinstance(methods, list):
            for m in methods:
                self._load_ui_methods(m)
        else:
            for name, fn in methods.items():
                if not name.startswith("_") and hasattr(fn, "__call__") \
                        and name[0].lower() == name[0]:
                    self.ui_methods[name] = fn

    def _load_ui_modules(self, modules):
        if isinstance(modules, types.ModuleType):
            self._load_ui_modules(dict((n, getattr(modules, n))
                                       for n in dir(modules)))
        elif isinstance(modules, list):
            for m in modules:
                self._load_ui_modules(m)
        else:
            # assert 断言
            assert isinstance(modules, dict)
            for name, cls in modules.items():
                try:
                    # issubclass 判断子类
                    if issubclass(cls, UIModule):
                        self.ui_modules[name] = cls
                except TypeError:
                    pass

    def __call__(self, request):
        """Called by HTTPServer to execute the request."""

        # 1、执行Application的 __call__ 方法时，其参数request是HTTPRequest对象
        # （其中封装HTTPConnetion、Stream、Application对象、请求头信息）
        # 2、Handler泛指就是我们定义的用于处理请求的类并且她还继承自RequestHandler

        transforms = [t(request) for t in self.transforms]
        handler = None
        args = []
        kwargs = {}

        # 第一步：取得请求的host的一组处理器
        # 根据请求的目标主机，匹配主机模版对应的正则表达式和Handlers
        handlers = self._get_host_handlers(request)
        if not handlers:
            handler = RedirectHandler(
                self, request, url="http://" + self.default_host + "/")
        else:
            for spec in handlers:
                match = spec.regex.match(request.path)  # 匹配请求的URI
                # 对于路径路由列表中的每一个对象，用 request.path 来匹配，如果匹配上了，
                # 就生成 RequestHandler 对象，并根据正则表达式解析路径里的参数，
                # 可以用数字做键值也可以用字符串做键值（详见：re.match.groups()）
                if match:
                    # #创建RquestHandler对象
                    handler = spec.handler_class(self, request, **spec.kwargs)  # 实例化，生成 RequestHandler 对象
                    if spec.regex.groups:   # 取得参数
                        # None-safe wrapper around url_unescape to handle
                        # unmatched optional groups correctly
                        def unquote(s):
                            if s is None:
                                return s
                            return escape.url_unescape(s, encoding=None,
                                                       plus=False)
                        # Pass matched groups to the handler.  Since
                        # match.groups() includes both named and unnamed groups,
                        # we want to use either groups or groupdict but not both.
                        # Note that args are passed as bytes so the handler can
                        # decide what encoding to use.

                        if spec.regex.groupindex:
                            kwargs = dict(
                                (str(k), unquote(v))
                                for (k, v) in match.groupdict().items())
                        else:
                            args = [unquote(s) for s in match.groups()]
                    break
            if not handler:
                handler = ErrorHandler(self, request, status_code=404)

        # In debug mode, re-compile templates and reload static files on every
        # request so you don't need to restart to see changes
        if self.settings.get("debug"):
            with RequestHandler._template_loader_lock:
                for loader in RequestHandler._template_loaders.values():
                    loader.reset()
            StaticFileHandler.reset()
        # 第三步：跳出列表，执行_execute()方法，
        # 这个方法在 RequestHandler 里被定义；含义
        # 是： 根据 http 方法转到对应方法，【路径正则表达式里】解析到的参数也被原样保留传进去。
        handler._execute(transforms, *args, **kwargs)  # 处理请求
        return handler

    def reverse_url(self, name, *args):
        """Returns a URL path for handler named ``name``

        The handler must be added to the application as a named `URLSpec`.

        Args will be substituted for capturing groups in the `URLSpec` regex.
        They will be converted to strings if necessary, encoded as utf8,
        and url-escaped.
        """
        if name in self.named_handlers:
            return self.named_handlers[name].reverse(*args)
        raise KeyError("%s not found in named urls" % name)

    def log_request(self, handler):
        """Writes a completed HTTP request to the logs.

        By default writes to the python root logger.  To change
        this behavior either subclass Application and override this method,
        or pass a function in the application settings dictionary as
        ``log_function``.
        """
        if "log_function" in self.settings:
            self.settings["log_function"](handler)
            return
        if handler.get_status() < 400:
            log_method = access_log.info
        elif handler.get_status() < 500:
            log_method = access_log.warning
        else:
            log_method = access_log.error
        request_time = 1000.0 * handler.request.request_time()
        log_method("%d %s %.2fms", handler.get_status(),
                   handler._request_summary(), request_time)


class HTTPError(Exception):
    """An exception that will turn into an HTTP error response.

    Raising an `HTTPError` is a convenient alternative to calling
    `RequestHandler.send_error` since it automatically ends the
    current function.

    :arg int status_code: HTTP status code.  Must be listed in
        `httplib.responses <http.client.responses>` unless the ``reason``
        keyword argument is given.
    :arg string log_message: Message to be written to the log for this error
        (will not be shown to the user unless the `Application` is in debug
        mode).  May contain ``%s``-style placeholders, which will be filled
        in with remaining positional parameters.
    :arg string reason: Keyword-only argument.  The HTTP "reason" phrase
        to pass in the status line along with ``status_code``.  Normally
        determined automatically from ``status_code``, but can be used
        to use a non-standard numeric code.
    """
    def __init__(self, status_code, log_message=None, *args, **kwargs):
        self.status_code = status_code
        self.log_message = log_message
        self.args = args
        self.reason = kwargs.get('reason', None)

    def __str__(self):
        message = "HTTP %d: %s" % (
            self.status_code,
            self.reason or httputil.responses.get(self.status_code, 'Unknown'))
        if self.log_message:
            return message + " (" + (self.log_message % self.args) + ")"
        else:
            return message


class MissingArgumentError(HTTPError):
    """Exception raised by `RequestHandler.get_argument`.

    This is a subclass of `HTTPError`, so if it is uncaught a 400 response
    code will be used instead of 500 (and a stack trace will not be logged).

    .. versionadded:: 3.1
    """
    def __init__(self, arg_name):
        super(MissingArgumentError, self).__init__(
            400, 'Missing argument %s' % arg_name)
        self.arg_name = arg_name


class ErrorHandler(RequestHandler):
    """Generates an error response with ``status_code`` for all requests."""
    def initialize(self, status_code):
        self.set_status(status_code)

    def prepare(self):
        raise HTTPError(self._status_code)

    def check_xsrf_cookie(self):
        # POSTs to an ErrorHandler don't actually have side effects,
        # so we don't need to check the xsrf token.  This allows POSTs
        # to the wrong url to return a 404 instead of 403.
        pass


class RedirectHandler(RequestHandler):
    """Redirects the client to the given URL for all GET requests.

    You should provide the keyword argument ``url`` to the handler, e.g.::

        application = web.Application([
            (r"/oldpath", web.RedirectHandler, {"url": "/newpath"}),
        ])
    """
    def initialize(self, url, permanent=True):
        self._url = url
        self._permanent = permanent

    def get(self):
        self.redirect(self._url, permanent=self._permanent)


class StaticFileHandler(RequestHandler):
    """A simple handler that can serve static content from a directory.

    A `StaticFileHandler` is configured automatically if you pass the
    ``static_path`` keyword argument to `Application`.  This handler
    can be customized with the ``static_url_prefix``, ``static_handler_class``,
    and ``static_handler_args`` settings.

    To map an additional path to this handler for a static data directory
    you would add a line to your application like::

        application = web.Application([
            (r"/content/(.*)", web.StaticFileHandler, {"path": "/var/www"}),
        ])

    The handler constructor requires a ``path`` argument, which specifies the
    local root directory of the content to be served.

    Note that a capture group in the regex is required to parse the value for
    the ``path`` argument to the get() method (different than the constructor
    argument above); see `URLSpec` for details.

    To maximize the effectiveness of browser caching, this class supports
    versioned urls (by default using the argument ``?v=``).  If a version
    is given, we instruct the browser to cache this file indefinitely.
    `make_static_url` (also available as `RequestHandler.static_url`) can
    be used to construct a versioned url.

    This handler is intended primarily for use in development and light-duty
    file serving; for heavy traffic it will be more efficient to use
    a dedicated static file server (such as nginx or Apache).  We support
    the HTTP ``Accept-Ranges`` mechanism to return partial content (because
    some browsers require this functionality to be present to seek in
    HTML5 audio or video), but this handler should not be used with
    files that are too large to fit comfortably in memory.

    **Subclassing notes**

    This class is designed to be extensible by subclassing, but because
    of the way static urls are generated with class methods rather than
    instance methods, the inheritance patterns are somewhat unusual.
    Be sure to use the ``@classmethod`` decorator when overriding a
    class method.  Instance methods may use the attributes ``self.path``
    ``self.absolute_path``, and ``self.modified``.

    To change the way static urls are generated (e.g. to match the behavior
    of another server or CDN), override `make_static_url`, `parse_url_path`,
    `get_cache_time`, and/or `get_version`.

    To replace all interaction with the filesystem (e.g. to serve
    static content from a database), override `get_content`,
    `get_content_size`, `get_modified_time`, `get_absolute_path`, and
    `validate_absolute_path`.

    .. versionchanged:: 3.1
       Many of the methods for subclasses were added in Tornado 3.1.
    """
    CACHE_MAX_AGE = 86400 * 365 * 10  # 10 years

    _static_hashes = {}
    _lock = threading.Lock()  # protects _static_hashes

    def initialize(self, path, default_filename=None):
        self.root = path
        self.default_filename = default_filename

    @classmethod
    def reset(cls):
        with cls._lock:
            cls._static_hashes = {}

    def head(self, path):
        self.get(path, include_body=False)

    def get(self, path, include_body=True):
        # Set up our path instance variables.
        self.path = self.parse_url_path(path)
        del path  # make sure we don't refer to path instead of self.path again
        absolute_path = self.get_absolute_path(self.root, self.path)
        self.absolute_path = self.validate_absolute_path(
            self.root, absolute_path)
        if self.absolute_path is None:
            return

        self.modified = self.get_modified_time()
        self.set_headers()

        if self.should_return_304():
            self.set_status(304)
            return

        request_range = None
        range_header = self.request.headers.get("Range")
        if range_header:
            # As per RFC 2616 14.16, if an invalid Range header is specified,
            # the request will be treated as if the header didn't exist.
            request_range = httputil._parse_request_range(range_header)

        if request_range:
            start, end = request_range
            size = self.get_content_size()
            if (start is not None and start >= size) or end == 0:
                # As per RFC 2616 14.35.1, a range is not satisfiable only: if
                # the first requested byte is equal to or greater than the
                # content, or when a suffix with length 0 is specified
                self.set_status(416)  # Range Not Satisfiable
                self.set_header("Content-Type", "text/plain")
                self.set_header("Content-Range", "bytes */%s" %(size, ))
                return
            if start is not None and start < 0:
                start += size
            if end is not None and end > size:
                # Clients sometimes blindly use a large range to limit their
                # download size; cap the endpoint at the actual file size.
                end = size
            # Note: only return HTTP 206 if less than the entire range has been
            # requested. Not only is this semantically correct, but Chrome
            # refuses to play audio if it gets an HTTP 206 in response to
            # ``Range: bytes=0-``.
            if size != (end or size) - (start or 0):
                self.set_status(206)  # Partial Content
                self.set_header("Content-Range",
                                httputil._get_content_range(start, end, size))
        else:
            start = end = None
        content = self.get_content(self.absolute_path, start, end)
        if isinstance(content, bytes_type):
            content = [content]
        content_length = 0
        for chunk in content:
            if include_body:
                self.write(chunk)
            else:
                content_length += len(chunk)
        if not include_body:
            assert self.request.method == "HEAD"
            self.set_header("Content-Length", content_length)

    def compute_etag(self):
        """Sets the ``Etag`` header based on static url version.

        This allows efficient ``If-None-Match`` checks against cached
        versions, and sends the correct ``Etag`` for a partial response
        (i.e. the same ``Etag`` as the full file).

        .. versionadded:: 3.1
        """
        version_hash = self._get_cached_version(self.absolute_path)
        if not version_hash:
            return None
        return '"%s"' % (version_hash, )

    def set_headers(self):
        """Sets the content and caching headers on the response.

        .. versionadded:: 3.1
        """
        self.set_header("Accept-Ranges", "bytes")
        self.set_etag_header()

        if self.modified is not None:
            self.set_header("Last-Modified", self.modified)

        content_type = self.get_content_type()
        if content_type:
            self.set_header("Content-Type", content_type)

        cache_time = self.get_cache_time(self.path, self.modified, content_type)
        if cache_time > 0:
            self.set_header("Expires", datetime.datetime.utcnow() +
                            datetime.timedelta(seconds=cache_time))
            self.set_header("Cache-Control", "max-age=" + str(cache_time))

        self.set_extra_headers(self.path)

    def should_return_304(self):
        """Returns True if the headers indicate that we should return 304.

        .. versionadded:: 3.1
        """
        if self.check_etag_header():
            return True

        # Check the If-Modified-Since, and don't send the result if the
        # content has not been modified
        ims_value = self.request.headers.get("If-Modified-Since")
        if ims_value is not None:
            date_tuple = email.utils.parsedate(ims_value)
            if date_tuple is not None:
                if_since = datetime.datetime(*date_tuple[:6])
                if if_since >= self.modified:
                    return True

        return False

    @classmethod
    def get_absolute_path(cls, root, path):
        """Returns the absolute location of ``path`` relative to ``root``.

        ``root`` is the path configured for this `StaticFileHandler`
        (in most cases the ``static_path`` `Application` setting).

        This class method may be overridden in subclasses.  By default
        it returns a filesystem path, but other strings may be used
        as long as they are unique and understood by the subclass's
        overridden `get_content`.

        .. versionadded:: 3.1
        """
        abspath = os.path.abspath(os.path.join(root, path))
        return abspath

    def validate_absolute_path(self, root, absolute_path):
        """Validate and return the absolute path.

        ``root`` is the configured path for the `StaticFileHandler`,
        and ``path`` is the result of `get_absolute_path`

        This is an instance method called during request processing,
        so it may raise `HTTPError` or use methods like
        `RequestHandler.redirect` (return None after redirecting to
        halt further processing).  This is where 404 errors for missing files
        are generated.

        This method may modify the path before returning it, but note that
        any such modifications will not be understood by `make_static_url`.

        In instance methods, this method's result is available as
        ``self.absolute_path``.

        .. versionadded:: 3.1
        """
        root = os.path.abspath(root)
        # os.path.abspath strips a trailing /
        # it needs to be temporarily added back for requests to root/
        if not (absolute_path + os.path.sep).startswith(root):
            raise HTTPError(403, "%s is not in root static directory",
                            self.path)
        if (os.path.isdir(absolute_path) and
                self.default_filename is not None):
            # need to look at the request.path here for when path is empty
            # but there is some prefix to the path that was already
            # trimmed by the routing
            if not self.request.path.endswith("/"):
                self.redirect(self.request.path + "/", permanent=True)
                return
            absolute_path = os.path.join(absolute_path, self.default_filename)
        if not os.path.exists(absolute_path):
            raise HTTPError(404)
        if not os.path.isfile(absolute_path):
            raise HTTPError(403, "%s is not a file", self.path)
        return absolute_path

    @classmethod
    def get_content(cls, abspath, start=None, end=None):
        """Retrieve the content of the requested resource which is located
        at the given absolute path.

        This class method may be overridden by subclasses.  Note that its
        signature is different from other overridable class methods
        (no ``settings`` argument); this is deliberate to ensure that
        ``abspath`` is able to stand on its own as a cache key.

        This method should either return a byte string or an iterator
        of byte strings.  The latter is preferred for large files
        as it helps reduce memory fragmentation.

        .. versionadded:: 3.1
        """
        with open(abspath, "rb") as file:
            if start is not None:
                file.seek(start)
            if end is not None:
                remaining = end - (start or 0)
            else:
                remaining = None
            while True:
                chunk_size = 64 * 1024
                if remaining is not None and remaining < chunk_size:
                    chunk_size = remaining
                chunk = file.read(chunk_size)
                if chunk:
                    if remaining is not None:
                        remaining -= len(chunk)
                    yield chunk
                else:
                    if remaining is not None:
                        assert remaining == 0
                    return

    @classmethod
    def get_content_version(cls, abspath):
        """Returns a version string for the resource at the given path.

        This class method may be overridden by subclasses.  The
        default implementation is a hash of the file's contents.

        .. versionadded:: 3.1
        """
        data = cls.get_content(abspath)
        hasher = hashlib.md5()
        if isinstance(data, bytes_type):
            hasher.update(data)
        else:
            for chunk in data:
                hasher.update(chunk)
        return hasher.hexdigest()

    def _stat(self):
        if not hasattr(self, '_stat_result'):
            self._stat_result = os.stat(self.absolute_path)
        return self._stat_result

    def get_content_size(self):
        """Retrieve the total size of the resource at the given path.

        This method may be overridden by subclasses. It will only
        be called if a partial result is requested from `get_content`

        .. versionadded:: 3.1
        """
        stat_result = self._stat()
        return stat_result[stat.ST_SIZE]

    def get_modified_time(self):
        """Returns the time that ``self.absolute_path`` was last modified.

        May be overridden in subclasses.  Should return a `~datetime.datetime`
        object or None.

        .. versionadded:: 3.1
        """
        stat_result = self._stat()
        modified = datetime.datetime.utcfromtimestamp(stat_result[stat.ST_MTIME])
        return modified

    def get_content_type(self):
        """Returns the ``Content-Type`` header to be used for this request.

        .. versionadded:: 3.1
        """
        mime_type, encoding = mimetypes.guess_type(self.absolute_path)
        return mime_type

    def set_extra_headers(self, path):
        """For subclass to add extra headers to the response"""
        pass

    def get_cache_time(self, path, modified, mime_type):
        """Override to customize cache control behavior.

        Return a positive number of seconds to make the result
        cacheable for that amount of time or 0 to mark resource as
        cacheable for an unspecified amount of time (subject to
        browser heuristics).

        By default returns cache expiry of 10 years for resources requested
        with ``v`` argument.
        """
        return self.CACHE_MAX_AGE if "v" in self.request.arguments else 0

    @classmethod
    def make_static_url(cls, settings, path, include_version=True):
        """Constructs a versioned url for the given path.

        This method may be overridden in subclasses (but note that it
        is a class method rather than an instance method).  Subclasses
        are only required to implement the signature
        ``make_static_url(cls, settings, path)``; other keyword
        arguments may be passed through `~RequestHandler.static_url`
        but are not standard.

        ``settings`` is the `Application.settings` dictionary.  ``path``
        is the static path being requested.  The url returned should be
        relative to the current host.

        ``include_version`` determines whether the generated URL should
        include the query string containing the version hash of the
        file corresponding to the given ``path``.

        """
        url = settings.get('static_url_prefix', '/static/') + path
        if not include_version:
            return url

        version_hash = cls.get_version(settings, path)
        if not version_hash:
            return url

        return '%s?v=%s' % (url, version_hash)

    def parse_url_path(self, url_path):
        """Converts a static URL path into a filesystem path.

        ``url_path`` is the path component of the URL with
        ``static_url_prefix`` removed.  The return value should be
        filesystem path relative to ``static_path``.

        This is the inverse of `make_static_url`.
        """
        if os.path.sep != "/":
            url_path = url_path.replace("/", os.path.sep)
        return url_path

    @classmethod
    def get_version(cls, settings, path):
        """Generate the version string to be used in static URLs.

        ``settings`` is the `Application.settings` dictionary and ``path``
        is the relative location of the requested asset on the filesystem.
        The returned value should be a string, or ``None`` if no version
        could be determined.

        .. versionchanged:: 3.1
           This method was previously recommended for subclasses to override;
           `get_content_version` is now preferred as it allows the base
           class to handle caching of the result.
        """
        abs_path = cls.get_absolute_path(settings['static_path'], path)
        return cls._get_cached_version(abs_path)

    @classmethod
    def _get_cached_version(cls, abs_path):
        with cls._lock:
            hashes = cls._static_hashes
            if abs_path not in hashes:
                try:
                    hashes[abs_path] = cls.get_content_version(abs_path)
                except Exception:
                    gen_log.error("Could not open static file %r", abs_path)
                    hashes[abs_path] = None
            hsh = hashes.get(abs_path)
            if hsh:
                return hsh
        return None


class FallbackHandler(RequestHandler):
    """A `RequestHandler` that wraps another HTTP server callback.

    The fallback is a callable object that accepts an
    `~.httpserver.HTTPRequest`, such as an `Application` or
    `tornado.wsgi.WSGIContainer`.  This is most useful to use both
    Tornado ``RequestHandlers`` and WSGI in the same server.  Typical
    usage::

        wsgi_app = tornado.wsgi.WSGIContainer(
            django.core.handlers.wsgi.WSGIHandler())
        application = tornado.web.Application([
            (r"/foo", FooHandler),
            (r".*", FallbackHandler, dict(fallback=wsgi_app),
        ])
    """
    def initialize(self, fallback):
        self.fallback = fallback

    def prepare(self):
        self.fallback(self.request)
        self._finished = True


class OutputTransform(object):
    """A transform modifies the result of an HTTP request (e.g., GZip encoding)

    A new transform instance is created for every request. See the
    ChunkedTransferEncoding example below if you want to implement a
    new Transform.
    """
    def __init__(self, request):
        pass

    def transform_first_chunk(self, status_code, headers, chunk, finishing):
        return status_code, headers, chunk

    def transform_chunk(self, chunk, finishing):
        return chunk


class GZipContentEncoding(OutputTransform):
    """Applies the gzip content encoding to the response.

    See http://www.w3.org/Protocols/rfc2616/rfc2616-sec14.html#sec14.11
    """
    CONTENT_TYPES = set([
        "text/plain", "text/html", "text/css", "text/xml", "application/javascript",
        "application/x-javascript", "application/xml", "application/atom+xml",
        "text/javascript", "application/json", "application/xhtml+xml"])
    MIN_LENGTH = 5

    def __init__(self, request):
        self._gzipping = request.supports_http_1_1() and \
            "gzip" in request.headers.get("Accept-Encoding", "")

    def transform_first_chunk(self, status_code, headers, chunk, finishing):
        if 'Vary' in headers:
            headers['Vary'] += b', Accept-Encoding'
        else:
            headers['Vary'] = b'Accept-Encoding'
        if self._gzipping:
            ctype = _unicode(headers.get("Content-Type", "")).split(";")[0]
            self._gzipping = (ctype in self.CONTENT_TYPES) and \
                (not finishing or len(chunk) >= self.MIN_LENGTH) and \
                (finishing or "Content-Length" not in headers) and \
                ("Content-Encoding" not in headers)
        if self._gzipping:
            headers["Content-Encoding"] = "gzip"
            self._gzip_value = BytesIO()
            self._gzip_file = gzip.GzipFile(mode="w", fileobj=self._gzip_value)
            chunk = self.transform_chunk(chunk, finishing)
            if "Content-Length" in headers:
                headers["Content-Length"] = str(len(chunk))
        return status_code, headers, chunk

    def transform_chunk(self, chunk, finishing):
        if self._gzipping:
            self._gzip_file.write(chunk)
            if finishing:
                self._gzip_file.close()
            else:
                self._gzip_file.flush()
            chunk = self._gzip_value.getvalue()
            self._gzip_value.truncate(0)
            self._gzip_value.seek(0)
        return chunk


class ChunkedTransferEncoding(OutputTransform):
    """Applies the chunked transfer encoding to the response.

    See http://www.w3.org/Protocols/rfc2616/rfc2616-sec3.html#sec3.6.1
    """
    def __init__(self, request):
        self._chunking = request.supports_http_1_1()

    def transform_first_chunk(self, status_code, headers, chunk, finishing):
        # 304 responses have no body (not even a zero-length body), and so
        # should not have either Content-Length or Transfer-Encoding headers.
        if self._chunking and status_code != 304:
            # No need to chunk the output if a Content-Length is specified
            if "Content-Length" in headers or "Transfer-Encoding" in headers:
                self._chunking = False
            else:
                headers["Transfer-Encoding"] = "chunked"
                chunk = self.transform_chunk(chunk, finishing)
        return status_code, headers, chunk

    def transform_chunk(self, block, finishing):
        if self._chunking:
            # Don't write out empty chunks because that means END-OF-STREAM
            # with chunked encoding
            if block:
                block = utf8("%x" % len(block)) + b"\r\n" + block + b"\r\n"
            if finishing:
                block += b"0\r\n\r\n"
        return block


def authenticated(method):
    """Decorate methods with this to require that the user be logged in.

    If the user is not logged in, they will be redirected to the configured
    `login url <RequestHandler.get_login_url>`.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        if not self.current_user:
            if self.request.method in ("GET", "HEAD"):
                url = self.get_login_url()
                if "?" not in url:
                    if urlparse.urlsplit(url).scheme:
                        # if login url is absolute, make next absolute too
                        next_url = self.request.full_url()
                    else:
                        next_url = self.request.uri
                    url += "?" + urlencode(dict(next=next_url))
                self.redirect(url)
                return
            raise HTTPError(403)
        return method(self, *args, **kwargs)
    return wrapper


class UIModule(object):
    """A re-usable, modular UI unit on a page.

    UI modules often execute additional queries, and they can include
    additional CSS and JavaScript that will be included in the output
    page, which is automatically inserted on page render.
    """
    def __init__(self, handler):
        self.handler = handler
        self.request = handler.request
        self.ui = handler.ui
        self.current_user = handler.current_user
        self.locale = handler.locale

    def render(self, *args, **kwargs):
        """Overridden in subclasses to return this module's output."""
        raise NotImplementedError()

    def embedded_javascript(self):
        """Returns a JavaScript string that will be embedded in the page."""
        return None

    def javascript_files(self):
        """Returns a list of JavaScript files required by this module."""
        return None

    def embedded_css(self):
        """Returns a CSS string that will be embedded in the page."""
        return None

    def css_files(self):
        """Returns a list of CSS files required by this module."""
        return None

    def html_head(self):
        """Returns a CSS string that will be put in the <head/> element"""
        return None

    def html_body(self):
        """Returns an HTML string that will be put in the <body/> element"""
        return None

    def render_string(self, path, **kwargs):
        """Renders a template and returns it as a string."""
        return self.handler.render_string(path, **kwargs)


class _linkify(UIModule):
    def render(self, text, **kwargs):
        return escape.linkify(text, **kwargs)


class _xsrf_form_html(UIModule):
    def render(self):
        return self.handler.xsrf_form_html()


class TemplateModule(UIModule):
    """UIModule that simply renders the given template.

    {% module Template("foo.html") %} is similar to {% include "foo.html" %},
    but the module version gets its own namespace (with kwargs passed to
    Template()) instead of inheriting the outer template's namespace.

    Templates rendered through this module also get access to UIModule's
    automatic javascript/css features.  Simply call set_resources
    inside the template and give it keyword arguments corresponding to
    the methods on UIModule: {{ set_resources(js_files=static_url("my.js")) }}
    Note that these resources are output once per template file, not once
    per instantiation of the template, so they must not depend on
    any arguments to the template.
    """
    def __init__(self, handler):
        super(TemplateModule, self).__init__(handler)
        # keep resources in both a list and a dict to preserve order
        self._resource_list = []
        self._resource_dict = {}

    def render(self, path, **kwargs):
        def set_resources(**kwargs):
            if path not in self._resource_dict:
                self._resource_list.append(kwargs)
                self._resource_dict[path] = kwargs
            else:
                if self._resource_dict[path] != kwargs:
                    raise ValueError("set_resources called with different "
                                     "resources for the same template")
            return ""
        return self.render_string(path, set_resources=set_resources,
                                  **kwargs)

    def _get_resources(self, key):
        return (r[key] for r in self._resource_list if key in r)

    def embedded_javascript(self):
        return "\n".join(self._get_resources("embedded_javascript"))

    def javascript_files(self):
        result = []
        for f in self._get_resources("javascript_files"):
            if isinstance(f, (unicode_type, bytes_type)):
                result.append(f)
            else:
                result.extend(f)
        return result

    def embedded_css(self):
        return "\n".join(self._get_resources("embedded_css"))

    def css_files(self):
        result = []
        for f in self._get_resources("css_files"):
            if isinstance(f, (unicode_type, bytes_type)):
                result.append(f)
            else:
                result.extend(f)
        return result

    def html_head(self):
        return "".join(self._get_resources("html_head"))

    def html_body(self):
        return "".join(self._get_resources("html_body"))


class _UIModuleNamespace(object):
    """Lazy namespace which creates UIModule proxies bound to a handler."""
    def __init__(self, handler, ui_modules):
        self.handler = handler
        self.ui_modules = ui_modules

    def __getitem__(self, key):
        return self.handler._ui_module(key, self.ui_modules[key])

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as e:
            raise AttributeError(str(e))


class URLSpec(object):
    """Specifies mappings between URLs and handlers."""
    def __init__(self, pattern, handler_class, kwargs=None, name=None):

        # Parameters:(参数)
        # * ``pattern``: Regular expression to be matched.  Any groups
        #   in the regex will be passed in to the handler's get/post/etc
        #   methods as arguments.（正则表达式匹配。
        # 在正则表达式的任何团体将传递给处理程序的GET/POST等方法作为参数。）
        #
        # * ``handler_class``: `RequestHandler` subclass to be invoked(子类被调用).
        #
        # * ``kwargs`` (optional): A dictionary of additional arguments
        #   to be passed to the handler's constructor（被传递到处理程序的构造函数）.
        #
        # * ``name`` (optional): A name for this handler.  Used by
        #   `Application.reverse_url（反向URL）`.

        if not pattern.endswith('$'):
            pattern += '$'
        self.regex = re.compile(pattern)
        # 没有看懂
        assert len(self.regex.groupindex) in (0, self.regex.groups), \
            ("groups in url regexes must either be all named or all "
             "positional: %r" % self.regex.pattern)
        self.handler_class = handler_class
        self.kwargs = kwargs or {}
        self.name = name

        # 返回URL的元组（反向字符串、组计数）
        self._path, self._group_count = self._find_groups()

    # 定义输出内容的（相当于改写了print输出函数）
    def __repr__(self):
        return '%s(%r, %s, kwargs=%r, name=%r)' % \
            (self.__class__.__name__, self.regex.pattern,
             self.handler_class, self.kwargs, self.name)

    def _find_groups(self):
        """Returns a tuple (reverse string, group count) for a url.

        For example: Given the url pattern /([0-9]{4})/([a-z-]+)/, this method
        would return ('/%s/%s/', 2).
        """
        pattern = self.regex.pattern
        # 先是对pattern这个字符串头部和尾部进行处理去掉(^$)的部分
        if pattern.startswith('^'):
            pattern = pattern[1:]
        if pattern.endswith('$'):
            pattern = pattern[:-1]
        # 2018/3/5 暂时不做深入研究
        if self.regex.groups != pattern.count('('):
            # The pattern is too complicated for our simplistic matching,
            # so we can't support reversing it.
            return (None, None)

        pieces = []
        for fragment in pattern.split('('):
            if ')' in fragment:
                paren_loc = fragment.index(')')
                if paren_loc >= 0:
                    pieces.append('%s' + fragment[paren_loc + 1:])
            else:
                pieces.append(fragment)

        return (''.join(pieces), self.regex.groups)

    def reverse(self, *args):
        assert self._path is not None, \
            "Cannot reverse url regex " + self.regex.pattern
        assert len(args) == self._group_count, "required number of arguments "\
            "not found"
        if not len(args):
            return self._path
        converted_args = []
        for a in args:
            if not isinstance(a, (unicode_type, bytes_type)):
                a = str(a)
            converted_args.append(escape.url_escape(utf8(a), plus=False))
        return self._path % tuple(converted_args)

url = URLSpec


if hasattr(hmac, 'compare_digest'):  # python 3.3
    _time_independent_equals = hmac.compare_digest
else:
    def _time_independent_equals(a, b):
        if len(a) != len(b):
            return False
        result = 0
        if isinstance(a[0], int):  # python3 byte strings
            for x, y in zip(a, b):
                result |= x ^ y
        else:  # python2
            for x, y in zip(a, b):
                result |= ord(x) ^ ord(y)
        return result == 0


def create_signed_value(secret, name, value):
    timestamp = utf8(str(int(time.time())))
    value = base64.b64encode(utf8(value))
    signature = _create_signature(secret, name, value, timestamp)
    value = b"|".join([value, timestamp, signature])
    return value


def decode_signed_value(secret, name, value, max_age_days=31):
    if not value:
        return None
    parts = utf8(value).split(b"|")
    if len(parts) != 3:
        return None
    signature = _create_signature(secret, name, parts[0], parts[1])
    if not _time_independent_equals(parts[2], signature):
        gen_log.warning("Invalid cookie signature %r", value)
        return None
    timestamp = int(parts[1])
    if timestamp < time.time() - max_age_days * 86400:
        gen_log.warning("Expired cookie %r", value)
        return None
    if timestamp > time.time() + 31 * 86400:
        # _cookie_signature does not hash a delimiter between the
        # parts of the cookie, so an attacker could transfer trailing
        # digits from the payload to the timestamp without altering the
        # signature.  For backwards compatibility, sanity-check timestamp
        # here instead of modifying _cookie_signature.
        gen_log.warning("Cookie timestamp in future; possible tampering %r", value)
        return None
    if parts[1].startswith(b"0"):
        gen_log.warning("Tampered cookie %r", value)
        return None
    try:
        return base64.b64decode(parts[0])
    except Exception:
        return None


def _create_signature(secret, *parts):
    hash = hmac.new(utf8(secret), digestmod=hashlib.sha1)
    for part in parts:
        hash.update(utf8(part))
    return utf8(hash.hexdigest())

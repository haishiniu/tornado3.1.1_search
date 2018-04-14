"""Miscellaneous utility functions and classes.

This module is used internally by Tornado.  It is not necessarily expected
that the functions and classes defined here will be useful to other
applications, but they are documented here in case they are.

The one public-facing part of this module is the `Configurable` class
and its `~Configurable.configure` method, which becomes a part of the
interface of its subclasses, including `.AsyncHTTPClient`, `.IOLoop`,
and `.Resolver`.
"""

from __future__ import absolute_import, division, print_function, with_statement

import inspect
import sys
import zlib


class ObjectDict(dict):
    """Makes a dictionary behave like an object, with attribute-style access.
    """
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        self[name] = value


class GzipDecompressor(object):
    """Streaming gzip decompressor.

    The interface is like that of `zlib.decompressobj` (without the
    optional arguments, but it understands gzip headers and checksums.
    """
    def __init__(self):
        # Magic parameter makes zlib module understand gzip header
        # http://stackoverflow.com/questions/1838699/how-can-i-decompress-a-gzip-stream-with-zlib
        # This works on cpython and pypy, but not jython.
        self.decompressobj = zlib.decompressobj(16 + zlib.MAX_WBITS)

    def decompress(self, value):
        """Decompress a chunk, returning newly-available data.

        Some data may be buffered for later processing; `flush` must
        be called when there is no more input data to ensure that
        all data was processed.
        """
        return self.decompressobj.decompress(value)

    def flush(self):
        """Return any remaining buffered data not yet returned by decompress.

        Also checks for errors such as truncated input.
        No other methods may be called on this object after `flush`.
        """
        return self.decompressobj.flush()


def import_object(name):
    """Imports an object by name.

    import_object('x') is equivalent to 'import x'.
    import_object('x.y.z') is equivalent to 'from x.y import z'.

    >>> import tornado.escape
    >>> import_object('tornado.escape') is tornado.escape
    True
    >>> import_object('tornado.escape.utf8') is tornado.escape.utf8
    True
    >>> import_object('tornado') is tornado
    True
    >>> import_object('tornado.missing_module')
    Traceback (most recent call last):
        ...
    ImportError: No module named missing_module
    """
    if name.count('.') == 0:
        return __import__(name, None, None)

    parts = name.split('.')
    obj = __import__('.'.join(parts[:-1]), None, None, [parts[-1]], 0)
    try:
        return getattr(obj, parts[-1])
    except AttributeError:
        raise ImportError("No module named %s" % parts[-1])


# Fake unicode literal support:  Python 3.2 doesn't have the u'' marker for
# literal strings, and alternative solutions like "from __future__ import
# unicode_literals" have other problems (see PEP 414).  u() can be applied
# to ascii strings that include \u escapes (but they must not contain
# literal non-ascii characters).
if type('') is not type(b''):
    def u(s):
        return s
    bytes_type = bytes
    unicode_type = str
    basestring_type = str
else:
    def u(s):
        return s.decode('unicode_escape')
    bytes_type = str
    unicode_type = unicode
    basestring_type = basestring


if sys.version_info > (3,):
    exec("""
def raise_exc_info(exc_info):
    raise exc_info[1].with_traceback(exc_info[2])

def exec_in(code, glob, loc=None):
    if isinstance(code, str):
        code = compile(code, '<string>', 'exec', dont_inherit=True)
    exec(code, glob, loc)
""")
else:
    exec("""
def raise_exc_info(exc_info):
    raise exc_info[0], exc_info[1], exc_info[2]

def exec_in(code, glob, loc=None):
    if isinstance(code, basestring):
        # exec(string) inherits the caller's future imports; compile
        # the string first to prevent that.
        code = compile(code, '<string>', 'exec', dont_inherit=True)
    exec code in glob, loc
""")


# Tornado3.0以后 IOLoop 模块的一些改动。

# IOLoop 成为 util.Configurable 的子类，IOLoop 中绝大多数成员方法都作为抽象接口，
# 具体实现由派生类 PollIOLoop 完成。IOLoop 实现了 Configurable 中的 configurable_base 和
#  configurable_default 这两个抽象接口，用于初始化过程中获取类类型和类的实现方法
# （即 IOLoop 中 poller 的实现方式）。


class Configurable(object):
    """Base class for configurable interfaces.

    A configurable interface is an (abstract) class whose constructor
    acts as a factory function for one of its implementation subclasses.
    The implementation subclass as well as optional keyword arguments to
    its initializer can be set globally at runtime with `configure`.

    By using the constructor as the factory method, the interface
    looks like a normal class, `isinstance` works as usual, etc.  This
    pattern is most useful when the choice of implementation is likely
    to be a global decision (e.g. when `~select.epoll` is available,
    always use it instead of `~select.select`), or when a
    previously-monolithic class has been split into specialized
    subclasses.

    Configurable subclasses must define the class methods
    `configurable_base` and `configurable_default`, and use the instance
    method `initialize` instead of ``__init__``.
    """
    __impl_class = None
    __impl_kwargs = None

    # 类里有一段注释，已经很明确的说明了它的设计意图和用法。
    # 它是可配置接口的父类，可配置接口对外提供一致的接口标识，
    # 但它的子类实现可以在运行时进行configure。
    # 一般在跨平台时由于子类实现有多种选择，这时候就可以使用可配置接口，
    # 例如select和epoll。
    # 首先注意 Configurable 的两个函数：
    # 1.configurable_base 和
    # 2.configurable_default，
    # 两函数都需要被子类（即可配置接口类）覆盖重写。
    # 其中，base函数一般【返回接口类自身】，
    # default【返回接口的默认子类实现】，
    # 除非接口指定了__impl_class。IOLoop及其子类实现都没有初始化函数也没有构造函数，
    # 其构造函数继承于Configurable，如下：

    def __new__(cls, **kwargs):

        # 当子类对象被构造时，子类__new__被调用，
        # 因此参数里的cls指的是Configurabel的子类（可配置接口类，如IOLoop）。
        # 先是得到base，查看IOLoop的代码发现它返回的是自身类。
        # 由于base和cls是一样的，所以调用configured_class()得到接口的子类实现，
        # 其实就是调用base（现在是IOLoop）的configurable_default，
        # 总之就是返回了一个子类实现（epoll/kqueue/select之一），
        # 顺便把__impl_kwargs合并到args里。
        # 接着把kwargs并到args里。然后调用Configurable的父类（Object）的__new__方法，
        # 生成了一个impl的对象，紧接着把args当参数调用该对象的initialize
        # （继承自PollIOloop，其initialize下段进行分析），返回该对象。

        # 所以，当构造IOLoop对象时，实际得到的是EPollIOLoop或其它相似子类。
        # 另外，Configurable 还提供configure方法来给接口指定实现子类和参数。
        # 可以看的出来，Configurable类主要提供【构造方法】，
        # 相当于【对象工厂】根据配置来生产对象，同时开放configure接口以供配置。
        # 而子类按照约定调整配置即可得到不同对象，代码得到了复用。
        base = cls.configurable_base()
        args = {}
        if cls is base:
            impl = cls.configured_class()
            if base.__impl_kwargs:
                args.update(base.__impl_kwargs)
        else:
            impl = cls
        args.update(kwargs)
        # 解决了构造，来看看IOLoop的instance方法。
        # 先检查类是否有成员_instance，一开始肯定没有，于是就构造了一个IOLoop对象（即EPollIOLoop对象）。
        # 以后如果再调用instance，得到的则是已有的对象，这样就确保了ioloop在【全局是单例】。
        # 再看epoll循环时注意到self._impl，
        # Configurable 和 IOLoop 里都没有，
        # 这是在哪儿定义的呢？
        # 为什么IOLoop的start跑到PollIOLoop里，应该是EPollIOLoop才对啊。
        # 对，应该看出来了，EPollIOLoop 就是PollIOLoop的子类，所以方法被继承了是很常见的哈。

        # 从上一段的构造流程里可以看到，EPollIOLoop对象的initialize方法被调用了，
        # 看其代码发现它调用了其父类（PollIOLoop）的它方法, 并指定了impl=select.epoll(),
        # 然后在父类的方法里就把它保存了下来，
        # 所以self._impl.poll就等效于select.epoll().poll().PollIOLoop里还有一些
        # 注册，修改，删除监听事件的方法，其实就是对self._impl的封装调用。
        # 就如上节的 add_accept_handler 就是调用ioloop的add_handler方法把监听fd和
        # accept_handler方法进行关联

        # IOLoop基本是个事件循环，因此它总是被其它模块所调用。而且为了足够通用，基本上对回调没多大限制，
        # 一个可执行对象即可。事件分发就到此结束了，和IO事件密切相关的另一个部分是IOStream，
        # 看看它是如何读写的。

        instance = super(Configurable, cls).__new__(impl)
        # initialize vs __init__ chosen for compatiblity with AsyncHTTPClient
        # singleton magic.  If we get rid of that we can switch to __init__
        # here too.
        instance.initialize(**args)
        return instance

    @classmethod
    def configurable_base(cls):
        """Returns the base class of a configurable hierarchy.

        This will normally return the class in which it is defined.
        (which is *not* necessarily the same as the cls classmethod parameter).
        """
        raise NotImplementedError()

    @classmethod
    def configurable_default(cls):
        """Returns the implementation class to be used if none is configured."""
        raise NotImplementedError()

    def initialize(self):
        """Initialize a `Configurable` subclass instance.

        Configurable classes should use `initialize` instead of ``__init__``.
        """

    @classmethod
    def configure(cls, impl, **kwargs):
        """Sets the class to use when the base class is instantiated.

        Keyword arguments will be saved and added to the arguments passed
        to the constructor.  This can be used to set global defaults for
        some parameters.
        """
        base = cls.configurable_base()
        if isinstance(impl, (unicode_type, bytes_type)):
            impl = import_object(impl)
        if impl is not None and not issubclass(impl, cls):
            raise ValueError("Invalid subclass of %s" % cls)
        base.__impl_class = impl
        base.__impl_kwargs = kwargs

    @classmethod
    def configured_class(cls):
        """Returns the currently configured class."""
        base = cls.configurable_base()
        if cls.__impl_class is None:
            base.__impl_class = cls.configurable_default()
        return base.__impl_class

    @classmethod
    def _save_configuration(cls):
        base = cls.configurable_base()
        return (base.__impl_class, base.__impl_kwargs)

    @classmethod
    def _restore_configuration(cls, saved):
        base = cls.configurable_base()
        base.__impl_class = saved[0]
        base.__impl_kwargs = saved[1]


class ArgReplacer(object):
    """Replaces one value in an ``args, kwargs`` pair.

    Inspects the function signature to find an argument by name
    whether it is passed by position or keyword.  For use in decorators
    and similar wrappers.
    """
    def __init__(self, func, name):
        self.name = name
        try:
            self.arg_pos = inspect.getargspec(func).args.index(self.name)
        except ValueError:
            # Not a positional parameter
            self.arg_pos = None

    def replace(self, new_value, args, kwargs):
        """Replace the named argument in ``args, kwargs`` with ``new_value``.

        Returns ``(old_value, args, kwargs)``.  The returned ``args`` and
        ``kwargs`` objects may not be the same as the input objects, or
        the input objects may be mutated.

        If the named argument was not found, ``new_value`` will be added
        to ``kwargs`` and None will be returned as ``old_value``.
        """
        if self.arg_pos is not None and len(args) > self.arg_pos:
            # The arg to replace is passed positionally
            old_value = args[self.arg_pos]
            args = list(args)  # *args is normally a tuple
            args[self.arg_pos] = new_value
        else:
            # The arg to replace is either omitted or passed by keyword.
            old_value = kwargs.get(self.name)
            kwargs[self.name] = new_value
        return old_value, args, kwargs


def doctests():
    import doctest
    return doctest.DocTestSuite()

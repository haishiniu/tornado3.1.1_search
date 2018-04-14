# -*- coding: utf-8 -*-
# !/usr/bin/python
import tornado.httpserver
import tornado.ioloop
import tornado.options
import tornado.web

from tornado.options import define, options
# 启动应用程序的时候指定一些参数; 选项参数的定义
# define函数是OptionParser类的成员，定义在tornado/options.py中，
# 机制与parse_command_line()类似。上面就是定义了一个int变量，名为port，默认值为8888，
# 还附带一个说明文字，厉害。port变量会被存放进options对象的一个dictionary成员中。
# 可以看到，访问方式就是options.port。执行完http_server.listen(options.port)，
# 你的PC上就会在options.port这个端口上启动一个服务器，开始侦听用户的连接。
define("port", default=8888, help="run on the given port", type=int)


class MainHandler(tornado.web.RequestHandler):
    # XXHandler 针对映射的 url 的具体实现
    # 它是tornado.web.RequestHandler的一个【子类】，覆盖了父类的get方法。
    # get方法也极简单，直接写一个“hello world”字符串到客户端。
    def get(self):
        self.write("Hello, Nowamagic\n")
        self.write('\n你好人类!!!!!!!!!')


def main():
    # 应用程序执行时，会先解析选择参数。之后创建一个Application
    # 实例并传递给HTTPServer实例，之后启动这个实例，到此，httpserver 启动了
    # tornado.httpserver 模块用来支持非阻塞的 HTTPServer
    tornado.options.parse_command_line()
    # 解析命令行参数，函数定义在tornado / options.py里;
    # 注意，在options.py里有两个parse_command_line()的定义;
    # 一个是OptionParser类的成员函数：
    # 另一个是普通的函数，不过它的实现是直接调用OptionParser的实现
    # 直接看OptionParser中的实现。简短的注释，parse_command_line()默认是解析sys.argv中的参数。
    application = tornado.web.Application([
        (r"/", MainHandler),
    ])
    # 这一句虽然简单，但信息量略大。首先，tornado.web.Application是一个tornado内置的类，
    # 定义在tornado/web.py中。从它大段的注释就可以看出来，这个类至少是男二号。按tornado的说法，
    # Application类实际是一些Handler的集合，这些Handler组成了一个web应用程序。
    #  what is handler ? 负责将客户请求的内容通过http协议返回给客户端。这些函数，就叫handler。
    # 上面这一句，是初始化了一个Application的实例，保存在application变量中。
    # Application的初始化参数是非常讲究的。在Application类的说明里，大段的话都是在讲参数的事。
    # 我们看Applicatino类的__init__函数原型：
    # def __init__(self, handlers=None, default_host="", transforms=None, wsgi=False, **settings):
    # 本例子只提供了handlers这个参数值。handlers实际上是一个列表，
    # 每个元素是（regexp，tornado.web.RequestHandler）这样的tuple。
    # 前面讲过，handler与用户请求之间存在映射关系，这个关联就是在这里定义的
    http_server = tornado.httpserver.HTTPServer(application)
    # 看起来我们是新建了一个http server的实例，前面创建好的application作为参数传递构了httpserver的构造函数。
    # HTTPServer类定义在tornado/httpserver.py中。显然这是男主角。它的注释说明比Application还要长，
    # 需要重点关注。
    http_server.listen(options.port)
    # 18-3.6
    tornado.ioloop.IOLoop.instance().start()
    # 对了，没有掉到关键的accept函数，用户连接又怎么处理呢？别急，马上就是。这一句看上去很玄乎的东西，
    # 其实就相当于我们熟悉的accept。
    # 那为什么不直接来个accept？这个IOLoop又是什么东西？
    # IOLoop与TCPServer之间的关系其实很简单。回忆用C语言写TCP服务器的情景，
    # 我们写好了create-bind-listen三段式后，其实事情还不算完。我们还得写点代码处理accept/recv/send呢。
    # 通常我们会写一个无限循环，不断调用accept来响应客户端链接。这个无限循环就是这里的IOLoop。
    # 这些代码让大家去写，最后其实都大同小异，因此tornado干脆写了一套标准代码，封装在IOLoop里。
    # 对于recv/send操作，通常也是在一个循环中进行的，也可以抽象成IOLoop。我们分析HTTPServer类的实现时，会看到它是怎么借助IOLoop工作的。
    # 到此为止我们的web server已经完备了。
    # 我们在浏览器里输入http://127.0.0.1:8888/，浏览器就会连接到我们的服务器，把HTTP请求送到HTTPServer中。
    # HTTPServer会先parse request，然后将reqeust交给第一个匹配到的Handler。Handler负责组织数据，
    # 调用发送API把数据传到客户端。


if __name__ == "__main__":
    main()
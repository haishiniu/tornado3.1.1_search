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

"""Utilities for working with multiple processes, including both forking
the server into multiple processes and managing subprocesses.
"""

from __future__ import absolute_import, division, print_function, with_statement

import errno
import multiprocessing
import os
import signal
import subprocess
import sys
import time

from binascii import hexlify

from tornado import ioloop
from tornado.iostream import PipeIOStream
from tornado.log import gen_log
from tornado.platform.auto import set_close_exec
from tornado import stack_context

try:
    long  # py2
except NameError:
    long = int  # py3


def cpu_count():
    """Returns the number of processors on this machine."""
    try:
        return multiprocessing.cpu_count()
    except NotImplementedError:
        pass
    try:
        return os.sysconf("SC_NPROCESSORS_CONF")
    except ValueError:
        pass
    gen_log.error("Could not detect number of processors; assuming 1")
    return 1


def _reseed_random():
    if 'random' not in sys.modules:
        return
    import random
    # If os.urandom is available, this method does the same thing as
    # random.seed (at least as of python 2.6).  If os.urandom is not
    # available, we mix in the pid in addition to a timestamp.
    try:
        seed = long(hexlify(os.urandom(16)), 16)
    except NotImplementedError:
        seed = int(time.time() * 1000) ^ os.getpid()
    random.seed(seed)


def _pipe_cloexec():
    r, w = os.pipe()
    set_close_exec(r)
    set_close_exec(w)
    return r, w


_task_id = None


def fork_processes(num_processes, max_restarts=100):
    """Starts multiple worker processes.

    If ``num_processes`` is None or <= 0, we detect the number of cores
    available on this machine and fork that number of child
    processes. If ``num_processes`` is given and > 0, we fork that
    specific number of sub-processes.

    Since we use processes and not threads, there is no shared memory
    between any server code.

    Note that multiple processes are not compatible with the autoreload
    module (or the debug=True option to `tornado.web.Application`).
    When using multiple processes, no IOLoops can be created or
    referenced until after the call to ``fork_processes``.

    In each child process, ``fork_processes`` returns its *task id*, a
    number between 0 and ``num_processes``.  Processes that exit
    abnormally (due to a signal or non-zero exit status) are restarted
    with the same id (up to ``max_restarts`` times).  In the parent
    process, ``fork_processes`` returns None if all child processes
    have exited normally, but will otherwise only exit by throwing an
    exception.
    """
    #

    global _task_id
    assert _task_id is None
    if num_processes is None or num_processes <= 0:
        num_processes = cpu_count()
    if ioloop.IOLoop.initialized():
        raise RuntimeError("Cannot run in multiple processes: IOLoop instance "
                           "has already been initialized. You cannot call "
                           "IOLoop.instance() before calling start_processes()")
    gen_log.info("Starting %d processes", num_processes)
    children = {}

    # 这一段很简单，就是在没有传入进程数的时候使用默认的cpu个数作为将要生成的进程个数。

    # 这是一个内函数，作用就是生成子进程。
    # 【fork】是个很有意思的方法，他会同时返回【两种状态】，为什么呢？
    # 其实fork相当于在原有的一条路（父进程）旁边又修了一条路（子进程）。
    # 如果这条路修成功了，那么在原有的路上（父进程）你就看到旁边来了另外一条路（子进程），
    # 所以也就是返回新生成的那条路的名字（子进程的pid），但是在另外一条路上（子进程），
    # 你看到的是自己本身修建成功了，也就返回自己的状态码（返回结果是0）。

    # 所以if pid==0表示这时候cpu已经切换到子进程了，相当于我们在新生成的这条路上面做事（返回任务id）；
    # else表示又跑到原来的路上做事了，在这里我们记录下新生成的子进程，
    # 这时候children[pid]=i里面的pid就是新生成的子进程的pid，
    # 而 i 就是刚才在子进程里面我们返回的任务id（其实就是用来代替子进程的id号）。

    def start_child(i):
        pid = os.fork()
        if pid == 0:
            # child process
            _reseed_random()
            global _task_id
            _task_id = i
            return i
        else:
            children[pid] = i
            return None

    # if id is not None表示如果我们在刚刚生成的那个子进程的上下文里面，那么就什么都不干，
    # 直接返回子进程的任务id就好了，啥都别想了，也别再折腾。
    # 如果还在父进程的上下文的话那么就继续生成子进程。
    for i in range(num_processes):
        id = start_child(i)
        if id is not None:
            return id
    num_restarts = 0
    while children:
        try:
            # pid, status = os.wait()的意思是等待任意子进程退出或者结束，
            # 这时候我们就把它从我们的children表里面去除掉，然后通过status判断子进程退出的原因。

            # 如果子进程是因为接收到kill信号或者抛出exception了，那么我们就重新启动一个子进程，
            # 用的当然还是刚刚退出的那个子进程的任务号。
            # 如果子进程是自己把事情做完了才退出的，那么就算了，等待别的子进程退出吧。
            pid, status = os.wait()
        except OSError as e:
            if e.errno == errno.EINTR:
                continue
            raise
        if pid not in children:
            continue
        id = children.pop(pid)
        if os.WIFSIGNALED(status):
            gen_log.warning("child %d (pid %d) killed by signal %d, restarting",
                            id, pid, os.WTERMSIG(status))
        elif os.WEXITSTATUS(status) != 0:
            gen_log.warning("child %d (pid %d) exited with status %d, restarting",
                            id, pid, os.WEXITSTATUS(status))
        else:
            gen_log.info("child %d (pid %d) exited normally", id, pid)
            continue
        num_restarts += 1
        if num_restarts > max_restarts:
            raise RuntimeError("Too many child restarts, giving up")
        # 我们看到在重新启动子进程的时候又使用了
        new_id = start_child(id)
        if new_id is not None:
            return new_id
    # All child processes exited cleanly, so exit the master process
    # instead of just returning to right after the call to
    # fork_processes (which will probably just start up another IOLoop
    # unless the caller checks the return value).
    sys.exit(0)


def task_id():
    """Returns the current task id, if any.

    Returns None if this process was not created by `fork_processes`.
    """
    global _task_id
    return _task_id


class Subprocess(object):
    """Wraps ``subprocess.Popen`` with IOStream support.

    The constructor is the same as ``subprocess.Popen`` with the following
    additions:

    * ``stdin``, ``stdout``, and ``stderr`` may have the value
      ``tornado.process.Subprocess.STREAM``, which will make the corresponding
      attribute of the resulting Subprocess a `.PipeIOStream`.
    * A new keyword argument ``io_loop`` may be used to pass in an IOLoop.
    """
    STREAM = object()

    _initialized = False
    _waiting = {}

    def __init__(self, *args, **kwargs):
        self.io_loop = kwargs.pop('io_loop', None) or ioloop.IOLoop.current()
        to_close = []
        if kwargs.get('stdin') is Subprocess.STREAM:
            in_r, in_w = _pipe_cloexec()
            kwargs['stdin'] = in_r
            to_close.append(in_r)
            self.stdin = PipeIOStream(in_w, io_loop=self.io_loop)
        if kwargs.get('stdout') is Subprocess.STREAM:
            out_r, out_w = _pipe_cloexec()
            kwargs['stdout'] = out_w
            to_close.append(out_w)
            self.stdout = PipeIOStream(out_r, io_loop=self.io_loop)
        if kwargs.get('stderr') is Subprocess.STREAM:
            err_r, err_w = _pipe_cloexec()
            kwargs['stderr'] = err_w
            to_close.append(err_w)
            self.stderr = PipeIOStream(err_r, io_loop=self.io_loop)
        self.proc = subprocess.Popen(*args, **kwargs)
        for fd in to_close:
            os.close(fd)
        for attr in ['stdin', 'stdout', 'stderr', 'pid']:
            if not hasattr(self, attr):  # don't clobber streams set above
                setattr(self, attr, getattr(self.proc, attr))
        self._exit_callback = None
        self.returncode = None

    def set_exit_callback(self, callback):
        """Runs ``callback`` when this process exits.

        The callback takes one argument, the return code of the process.

        This method uses a ``SIGCHILD`` handler, which is a global setting
        and may conflict if you have other libraries trying to handle the
        same signal.  If you are using more than one ``IOLoop`` it may
        be necessary to call `Subprocess.initialize` first to designate
        one ``IOLoop`` to run the signal handlers.

        In many cases a close callback on the stdout or stderr streams
        can be used as an alternative to an exit callback if the
        signal handler is causing a problem.
        """
        self._exit_callback = stack_context.wrap(callback)
        Subprocess.initialize(self.io_loop)
        Subprocess._waiting[self.pid] = self
        Subprocess._try_cleanup_process(self.pid)

    @classmethod
    def initialize(cls, io_loop=None):
        """Initializes the ``SIGCHILD`` handler.

        The signal handler is run on an `.IOLoop` to avoid locking issues.
        Note that the `.IOLoop` used for signal handling need not be the
        same one used by individual Subprocess objects (as long as the
        ``IOLoops`` are each running in separate threads).
        """
        if cls._initialized:
            return
        if io_loop is None:
            io_loop = ioloop.IOLoop.current()
        cls._old_sigchld = signal.signal(
            signal.SIGCHLD,
            lambda sig, frame: io_loop.add_callback_from_signal(cls._cleanup))
        cls._initialized = True

    @classmethod
    def uninitialize(cls):
        """Removes the ``SIGCHILD`` handler."""
        if not cls._initialized:
            return
        signal.signal(signal.SIGCHLD, cls._old_sigchld)
        cls._initialized = False

    @classmethod
    def _cleanup(cls):
        for pid in list(cls._waiting.keys()):  # make a copy
            cls._try_cleanup_process(pid)

    @classmethod
    def _try_cleanup_process(cls, pid):
        try:
            ret_pid, status = os.waitpid(pid, os.WNOHANG)
        except OSError as e:
            if e.args[0] == errno.ECHILD:
                return
        if ret_pid == 0:
            return
        assert ret_pid == pid
        subproc = cls._waiting.pop(pid)
        subproc.io_loop.add_callback_from_signal(
            subproc._set_returncode, status)

    def _set_returncode(self, status):
        if os.WIFSIGNALED(status):
            self.returncode = -os.WTERMSIG(status)
        else:
            assert os.WIFEXITED(status)
            self.returncode = os.WEXITSTATUS(status)
        if self._exit_callback:
            callback = self._exit_callback
            self._exit_callback = None
            callback(self.returncode)

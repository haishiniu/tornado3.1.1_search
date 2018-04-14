``tornado.ioloop`` --- Main event loop
======================================

.. automodule:: tornado.ioloop

   IOLoop objects
   --------------

   .. autoclass:: IOLoop

   Running an IOLoop
   ^^^^^^^^^^^^^^^^^

   .. automethod:: IOLoop.current
   .. automethod:: IOLoop.make_current
   .. automethod:: IOLoop.instance
   .. automethod:: IOLoop.initialized
   .. automethod:: IOLoop.install
   .. automethod:: IOLoop.start
   .. automethod:: IOLoop.stop
   .. automethod:: IOLoop.run_sync
   .. automethod:: IOLoop.close

   I/O events
   ^^^^^^^^^^

   .. automethod:: IOLoop.add_handler
   .. automethod:: IOLoop.update_handler
   .. automethod:: IOLoop.remove_handler

   Callbacks and timeouts
   ^^^^^^^^^^^^^^^^^^^^^^

   .. automethod:: IOLoop.add_callback
   .. automethod:: IOLoop.add_callback_from_signal
   .. automethod:: IOLoop.add_future
   .. automethod:: IOLoop.add_timeout
   .. automethod:: IOLoop.remove_timeout
   .. automethod:: IOLoop.time
   .. autoclass:: PeriodicCallback
      :members:

   Debugging and error handling
   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^

   .. automethod:: IOLoop.handle_callback_exception
   .. automethod:: IOLoop.set_blocking_signal_threshold
   .. automethod:: IOLoop.set_blocking_log_threshold
   .. automethod:: IOLoop.log_stack

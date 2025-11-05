# Copyright (c) 2025
#
# All rights reserved.
#
# Dedicated thread pools for separating BlockProcessor and client operations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial


class ThreadPools:
    '''Manages separate thread pools for different server operations.
    
    This prevents client requests from starving BlockProcessor flush operations.
    '''
    
    def __init__(self, bp_workers=20, client_workers=50):
        '''Create two separate thread pools.
        
        Args:
            bp_workers: Number of threads dedicated to BlockProcessor operations (flush, backup)
            client_workers: Number of threads for client requests (read_history, read_headers)
        '''
        self.bp_executor = ThreadPoolExecutor(
            max_workers=bp_workers,
            thread_name_prefix='BlockProcessor'
        )
        self.client_executor = ThreadPoolExecutor(
            max_workers=client_workers,
            thread_name_prefix='ClientRequest'
        )
        self._loop = None
        
    def setup(self, loop):
        '''Set up the executors with the event loop.
        
        Args:
            loop: The asyncio event loop
        '''
        self._loop = loop
        # Set client executor as default for backwards compatibility
        loop.set_default_executor(self.client_executor)
        
    async def run_in_bp_thread(self, func, *args):
        '''Run a function in the BlockProcessor thread pool.
        
        This should be used for:
        - advance_block
        - flush_dbs
        - backup_block
        '''
        if self._loop is None:
            raise RuntimeError('ThreadPools not set up. Call setup() first.')
        if args:
            func = partial(func, *args)
        return await self._loop.run_in_executor(self.bp_executor, func)
    
    async def run_in_client_thread(self, func, *args):
        '''Run a function in the client request thread pool.
        
        This should be used for:
        - read_history
        - read_headers
        - lookup_utxos
        - All other DB read operations triggered by client requests
        '''
        if self._loop is None:
            raise RuntimeError('ThreadPools not set up. Call setup() first.')
        if args:
            func = partial(func, *args)
        return await self._loop.run_in_executor(self.client_executor, func)
    
    def shutdown(self):
        '''Shutdown both thread pools.'''
        self.bp_executor.shutdown(wait=False)
        self.client_executor.shutdown(wait=False)


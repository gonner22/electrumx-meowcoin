# Copyright (c) 2016-2017, Neil Booth
# Copyright (c) 2017, the ElectrumX authors
#
# All rights reserved.
#
# See the file "LICENCE" for information about the copyright
# and warranty status of this software.

'''Block prefetcher and chain processor.'''

import asyncio
import os
import re
import hashlib
import logging
import os
import pylru
import traceback
import time
from collections import defaultdict
from datetime import datetime
from asyncio import sleep
from struct import error as struct_error
from typing import Callable, Dict, Optional, List

from aiorpcx import CancelledError, run_in_thread, spawn

import electrumx
from electrumx.lib.addresses import public_key_to_address
from electrumx.lib.hash import hash_to_hex_str, HASHX_LEN, double_sha256
from electrumx.lib.script import is_unspendable_legacy, \
    is_unspendable_genesis, OpCodes, Script, ScriptError
from electrumx.lib.tx import Deserializer
from electrumx.lib.util import (
    class_logger, pack_le_uint32, pack_le_uint64, unpack_le_uint64, base_encode, DataParser, 
    open_file, unpack_le_uint32
)
from electrumx.server.db import FlushData, DB
from electrumx.server.db import (
    NULL_U32, NULL_TXNUMB, PREFIX_METADATA, PREFIX_VERIFIER_CURRENT, PREFIX_VERIFIER_HISTORY,
    PREFIX_ASSOCIATION_CURRENT, PREFIX_FREEZE_CURRENT, PREFIX_ASSET_TAG_CURRENT, PREFIX_ASSET_ID_UNDO,
    PREFIX_H160_ID_UNDO, PREFIX_ASSET_TO_ID, PREFIX_ID_TO_ASSET, PREFIX_H160_TO_ID, PREFIX_ID_TO_H160,
    PREFIX_METADATA_HISTORY, PREFIX_BROADCAST, PREFIX_H160_TAG_CURRENT, PREFIX_ASSET_TAG_HISTORY, 
    PREFIX_H160_TAG_HISTORY, PREFIX_ASSOCIATION_HISTORY, PREFIX_FREEZE_HISTORY, PREFIX_UTXO_HISTORY,
    PREFIX_HASHX_LOOKUP
)
from electrumx.server.env import Env
from electrumx.server.daemon import Daemon


class OPPushDataGeneric:
    def __init__(self, pushlen: Callable = None):
        if pushlen is not None:
            self.check_data_len = pushlen

    @classmethod
    def check_data_len(cls, datalen: int) -> bool:
        # Opcodes below OP_PUSHDATA4 all just push data onto stack, and are
        return OpCodes.OP_PUSHDATA4 >= datalen >= 0

    @classmethod
    def is_instance(cls, item):
        # accept objects that are instances of this class
        # or other classes that are subclasses
        return isinstance(item, cls) \
               or (isinstance(item, type) and issubclass(item, cls))


# Marks an address as valid for restricted assets via qualifier or restricted itself.
ASSET_NULL_TEMPLATE = [OpCodes.OP_MEWC_ASSET, OPPushDataGeneric(lambda x: x == 20), OPPushDataGeneric()]
# Used with creating restricted assets. Dictates the qualifier assets associated.
ASSET_NULL_VERIFIER_TEMPLATE = [OpCodes.OP_MEWC_ASSET, OpCodes.OP_RESERVED, OPPushDataGeneric()]
# Stop all movements of a restricted asset.
ASSET_GLOBAL_RESTRICTION_TEMPLATE = [OpCodes.OP_MEWC_ASSET, OpCodes.OP_RESERVED, OpCodes.OP_RESERVED,
                                     OPPushDataGeneric()]


# -1 if doesn't match, positive if does. Indicates index in script
def match_script_against_template(script, template) -> int:
    """Returns whether 'script' matches 'template'."""
    if script is None:
        return -1
    if len(script) < len(template):
        return -1
    ctr = 0
    for i in range(len(template)):
        ctr += 1
        template_item = template[i]
        script_item = script[i]
        if OPPushDataGeneric.is_instance(template_item) and template_item.check_data_len(script_item[0]):
            continue
        if template_item != script_item[0]:
            return -1
    return ctr

logger = class_logger(__name__, 'BlockProcessor')


class OnDiskBlock:

    path = 'meta/blocks'
    del_regex = re.compile('([0-9a-f]{64}\\.tmp)$')
    legacy_del_regex = re.compile('block[0-9]{1,7}$')
    block_regex = re.compile('([0-9]{1,8})-([0-9a-f]{64})$')
    chunk_size = 25_000_000
    # On-disk blocks. hex_hash->(height, size) pair
    blocks = {}
    # Map from hex hash to prefetch task
    tasks = {}
    # If set it logs the next time a block is processed
    log_block = False
    daemon = None
    state = None

    def __init__(self, coin, hex_hash, height, size):
        self.hex_hash = hex_hash
        self.coin = coin
        self.height = height
        self.size = size
        self.block_file = None
        self.header = None
        self.header_end_offset = None  # Position after header where transactions start

    @classmethod
    def filename(cls, hex_hash, height):
        return os.path.join(cls.path, f'{height:d}-{hex_hash}')

    def __enter__(self):
        self.block_file = open_file(self.filename(self.hex_hash, self.height))
        
        # FIXED: After AuxPOW activation, blocks can be:
        # 1. Mined directly (MeowPow): version bit set, but NO AuxPOW structure
        # 2. Merge-mined (Scrypt): version bit set AND has AuxPOW structure
        # The version bit only indicates AuxPOW is enabled, not that this specific block has it
        if self.coin.is_auxpow_active(self.height):
            try:
                # Peek at version to check version bit
                peek_data = self.block_file.read(4)
                if len(peek_data) < 4:
                    raise RuntimeError(f'Cannot read version from block {self.hex_hash}')
                self.block_file.seek(0)
                version_int = int.from_bytes(peek_data[:4], byteorder='little')
                
                if self.coin.is_auxpow_block(version_int):  # AuxPOW version bit is set
                    # Try to parse as AuxPOW block first
                    # If it fails, it's a MeowPow direct block without AuxPOW structure
                    from electrumx.lib.tx import DeserializerAuxPow
                    peek_size = min(50000, self.size)
                    raw_block_peek = self.block_file.read(peek_size)
                    
                    if len(raw_block_peek) < 80:
                        # Block too small, treat as normal block
                        self.block_file.seek(0)
                    else:
                        try:
                            deserializer = DeserializerAuxPow(raw_block_peek)
                            # Try to read header - this will fail if no AuxPOW structure exists
                            self.header = deserializer.read_header(self.coin.BASIC_HEADER_SIZE, self.height)
                            # If we got here, AuxPOW structure exists
                            header_end_offset = deserializer.cursor
                            
                            if header_end_offset > self.size:
                                raise RuntimeError(f'AuxPOW header parsing error: cursor {header_end_offset} exceeds block size {self.size}')
                            
                            self.header_end_offset = header_end_offset
                            self.block_file.seek(header_end_offset)
                            return self
                        except (ValueError, IndexError, RuntimeError):
                            # Failed to parse AuxPOW structure - this is a MeowPow direct block
                            # According to Meowcoin code: if nVersion.IsAuxpow() but no AuxPOW structure,
                            # header is 80 bytes (includes nNonce but not nHeight/nNonce64/mix_hash)
                            logger.debug(f'Block {self.hex_hash} height {self.height}: AuxPOW bit set but no structure, treating as MeowPow direct (80-byte header)')
                            self.block_file.seek(0)
                            self.header = self._read(80)
                            self.header_end_offset = 80
                            return self
            except Exception:
                # Exception in AuxPOW detection, reset file position and fall through to pre-AuxPOW path
                self.block_file.seek(0)
        
        # For blocks before AuxPOW activation, use static header length
        header_len = self.coin.static_header_len(self.height)
        self.header = self._read(header_len)
        self.header_end_offset = header_len
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.block_file.close()

    def _read(self, size):
        result = self.block_file.read(size)
        # Allow EOF (empty result) when we've already read some data
        # Only raise error if file is completely empty from the start
        if not result and self.block_file.tell() == 0:
            raise RuntimeError(f'empty block file for block {self.hex_hash} '
                               f'height {self.height:,d}')
        return result

    def _read_at_pos(self, pos, size):
        self.block_file.seek(pos, os.SEEK_SET)
        result = self.block_file.read(size)
        if len(result) != size:
            raise RuntimeError(f'truncated block file for block {self.hex_hash} '
                               f'height {self.height:,d}')
        return result

    def date_str(self):
        timestamp, = unpack_le_uint32(self.header[68:72])
        return datetime.utcfromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')

    def iter_txs(self):
        # Generator of (tx, tx_hash) pairs
        raw = self._read(self.chunk_size)
        deserializer = self.coin.DESERIALIZER(raw)
        tx_count = deserializer.read_varint()

        if self.log_block:
            logger.info(f'height {self.height:,d} of {self.daemon.cached_height():,d} '
                        f'{self.hex_hash} {self.date_str()} '
                        f'{self.size / 1_000_000:.3f}MB {tx_count:,d} txs '
                        f'chain {self.state.chain_size / 1_000_000_000:.3f}GB')
            OnDiskBlock.log_block = False

        count = 0
        while True:
            read = deserializer.read_tx_and_hash
            try:
                while True:
                    cursor = deserializer.cursor
                    yield read()
                    count += 1
            except (AssertionError, IndexError, struct_error):
                pass

            if tx_count == count:
                return
                
            raw = raw[cursor:] + self._read(self.chunk_size)
            deserializer = self.coin.DESERIALIZER(raw)

    def _chunk_offsets(self):
        '''Iterate the transactions forwards to find their boundaries.'''
        base_offset = self.block_file.tell()
        # CRITICAL FIX: For AuxPOW blocks, cursor can be at variable positions
        # Don't assert fixed offsets - the __enter__ method positions correctly
        # Just verify we're past the header (at least 80 bytes)
        if base_offset < 80:
            raise RuntimeError(f'Invalid base_offset {base_offset} - must be at least 80 (after header)')
        raw = self._read(self.chunk_size)
        if not raw:
            raise RuntimeError(f'No transaction data after header at offset {base_offset} '
                               f'for block {self.hex_hash} height {self.height:,d} size {self.size}')
        deserializer = Deserializer(raw)
        tx_count = deserializer.read_varint()
        logger.info(f'backing up block {self.hex_hash} height {self.height:,d} '
                    f'tx_count {tx_count:,d}')
        offsets = [base_offset + deserializer.cursor]

        while True:
            read = deserializer.read_tx
            count = 0
            try:
                while True:
                    cursor = deserializer.cursor
                    read()
                    count += 1
            except (AssertionError, IndexError, struct_error):
                pass

            if count:
                offsets.append(base_offset + cursor)
                base_offset += cursor
            tx_count -= count
            if tx_count == 0:
                return offsets
            raw = raw[cursor:] + self._read(self.chunk_size)
            if not raw:
                raise RuntimeError(f'Incomplete block data: {tx_count} transactions remaining '
                                   f'for block {self.hex_hash} height {self.height:,d}')
            deserializer = Deserializer(raw)

    def iter_txs_reversed(self):
        # Iterate the block transactions in reverse order.  We need to iterate the
        # transactions forwards first to find their boundaries.
        offsets = self._chunk_offsets()
        for n in reversed(range(len(offsets) - 1)):
            start = offsets[n]
            size = offsets[n + 1] - start
            deserializer = Deserializer(self._read_at_pos(start, size))
            pairs = []
            while deserializer.cursor < size:
                pairs.append(deserializer.read_tx_and_hash())
            for item in reversed(pairs):
                yield item

    @classmethod
    async def delete_stale(cls, items, log):
        def delete(paths):
            count = total_size = 0
            for path, size in paths.items():
                try:
                    os.remove(path)
                    count += 1
                    total_size += size
                except FileNotFoundError as e:
                    logger.error(f'could not delete stale block file {path}: {e}')
            return count, total_size

        if not items:
            return
        paths = {}
        for item in items:
            if isinstance(item, os.DirEntry):
                paths[item.path] = item.stat().st_size
            else:
                height, size = cls.blocks.pop(item)
                paths[cls.filename(item, height)] = size

        count, total_size = await run_in_thread(delete, paths)
        if log:
            logger.info(f'deleted {count:,d} stale block files, total size {total_size:,d} bytes')

    @classmethod
    async def delete_blocks(cls, min_height, log):
        blocks_to_delete = [hex_hash for hex_hash, (height, size) in cls.blocks.items()
                            if height < min_height]
        await cls.delete_stale(blocks_to_delete, log)

    @classmethod
    async def scan_files(cls):
        # Remove stale block files
        def scan():
            to_delete = []
            with os.scandir(cls.path) as it:
                for dentry in it:
                    if dentry.is_file():
                        match = cls.block_regex.match(dentry.name)
                        if match:
                            to_delete.append(dentry)
            return to_delete

        def find_legacy_blocks():
            with os.scandir('meta') as it:
                return [dentry for dentry in it
                        if dentry.is_file() and cls.legacy_del_regex.match(dentry.name)]

        try:
            # This only succeeds the first time with the new code
            os.mkdir(cls.path)
            logger.info(f'created block directory {cls.path}')
            await cls.delete_stale(await run_in_thread(find_legacy_blocks), True)
        except FileExistsError:
            pass

        logger.info(f'scanning block directory {cls.path}...')
        to_delete = await run_in_thread(scan)
        await cls.delete_stale(to_delete, True)

    @classmethod
    async def prefetch_many(cls, daemon, pairs, kind):
        async def prefetch_one(hex_hash, height):
            '''Read a block in chunks to a temporary file.  Rename the file only when done so
            as not to have incomplete blocks considered complete.
            '''
            try:
                filename = cls.filename(hex_hash, height)
                size = await daemon.get_block(hex_hash, filename)
                cls.blocks[hex_hash] = (height, size)
                if kind == 'new':
                    logger.info(f'fetched new block height {height:,d} hash {hex_hash}')
                elif kind == 'reorg':
                    logger.info(f'fetched reorged block height {height:,d} hash {hex_hash}')
            except Exception as e:
                logger.error(f'error prefetching {hex_hash}: {e}')
            finally:
                cls.tasks.pop(hex_hash)

        # Pairs is a (height, hex_hash) iterable
        for height, hex_hash in pairs:
            if hex_hash not in cls.tasks and hex_hash not in cls.blocks:
                cls.tasks[hex_hash] = await spawn(prefetch_one, hex_hash, height)

    @classmethod
    async def streamed_block(cls, coin, hex_hash):
        # Waits for a block to come in.
        task = cls.tasks.get(hex_hash)
        if task:
            await task
        item = cls.blocks.get(hex_hash)
        if not item:
            logger.error(f'block {hex_hash} missing')            
            return None
        height, size = item
        return cls(coin, hex_hash, height, size)

    @classmethod
    async def stop_prefetching(cls):
        for task in cls.tasks.values():
            task.cancel()
        logger.info('prefetcher stopped')


class ChainError(Exception):
    '''Raised on error processing blocks.'''


class BlockProcessor:
    '''Process blocks and update the DB state to match.  Prefetch blocks so they are
    immediately available when the processor is ready for a new block.  Coordinate backing
    up in case of chain reorganisations.
    '''

    polling_delay = 3  # Reduced from 5 to 3 for faster block detection

    def __init__(self, env: Env, db: DB, daemon: Daemon, notifications):
        self.env = env
        self.db = db
        self.daemon = daemon
        self.notifications = notifications

        self.bad_vouts_path = os.path.join(self.env.db_dir, 'invalid_chain_vouts')

        self.coin = env.coin
        
        # Meta
        self.caught_up = False
        self.ok = True
        self.touched = set()
        # A count >= 0 is a user-forced reorg; < 0 is a natural reorg
        self.reorg_count = None
        self.force_flush_arg = None
        self.processing_blocks = False  # Track if advance_blocks() is processing

         # State.  Initially taken from DB;
        self.state = None

        # Caches of unflushed items.
        self.headers = []
        self.tx_hashes = []

        # UTXO cache
        self.utxo_cache = {}
        self.utxo_deletes = []
        self.utxo_undos = []

        # Asset ID cache
        self.new_asset_ids = {}
        self.new_asset_ids_undos = []
        self.asset_ids_deletes = []

        # H160 ID cache
        self.new_h160_ids = {}
        self.new_h160_ids_undos = []
        self.h160_ids_deletes = []

        # Metadata
        self.asset_metadata = {}
        self.asset_metadata_undos = []
        self.asset_metadata_deletes = []
        self.asset_metadata_history = {}
        self.asset_metadata_history_undos = []
        self.asset_metadata_history_deletes = []

        # Broadcasts
        self.asset_broadcasts = {}
        self.asset_broadcasts_undos = []
        self.asset_broadcasts_deletes = []

        # Tags
        self.tags = {}
        self.tags_undos = []
        self.tags_deletes = []
        self.tag_history = {}
        self.tag_history_undos = []
        self.tag_history_deletes = []

        # Freezes
        self.freezes = {}
        self.freezes_undos = []
        self.freezes_deletes = []
        self.freeze_history = {}
        self.freeze_history_undos = []
        self.freeze_history_deletes = []

        # Verifier
        self.verifiers = {}
        self.verifiers_undos = []
        self.verifiers_deletes = []
        self.verifier_history = {}
        self.verifier_history_undos = []
        self.verifier_history_deletes = []

        # Associations
        self.associations = {}
        self.associations_undos = []
        self.associations_deletes = []
        self.association_history = {}
        self.association_history_undos = []
        self.association_history_deletes = []

        # To notify clients about reissuances
        self.asset_touched = set()
        # To notify when a qualifier has tagged/revoked a h160
        self.qualifier_touched = set()
        # To notify when a h160 has been tagged/revoked by a qualifier
        self.h160_touched = set()
        # To notify when a broadcast has been made
        self.broadcast_touched = set()
        # To notify when a restricted asset has been frozen
        self.frozen_touched = set()
        # To notify when a validator string has been changed
        self.validator_touched = set()
        # To notify when a qualifier has become a part/removed from a validator string
        self.qualifier_association_touched = set()
        
        self.backed_up_event = asyncio.Event()

        # When the lock is acquired, in-memory chain state is consistent with state.height.
        # This is a requirement for safe flushing.
        self.state_lock = asyncio.Lock()

    async def run_with_lock(self, coro):
        # Shielded so that cancellations from shutdown don't lose work.  Cancellation will
        # cause fetch_and_process_blocks to block on the lock in flush(), the task completes,
        # and then the data is flushed.  We also don't want user-signalled reorgs to happen
        # in the middle of processing blocks; they need to wait.
        async def run_locked():
            async with self.state_lock:
                return await coro
        return await asyncio.shield(run_locked())

    async def next_block_hashes(self):
        daemon_height = await self.daemon.height()
        first = self.state.height + 1
        count = min(daemon_height - first + 1, self.coin.prefetch_limit(first))
        if count:
            hex_hashes = await self.daemon.block_hex_hashes(first, count)
            kind = 'new' if self.caught_up else 'sync'
            await OnDiskBlock.prefetch_many(self.daemon, enumerate(hex_hashes, start=first), kind)
        else:
            hex_hashes = []

        # Remove stale blocks
        await OnDiskBlock.delete_blocks(first - 5, False)

        return hex_hashes[:(count + 1) // 2], daemon_height

    async def reorg_chain(self, count):
        '''Handle a chain reorganisation.

        Count is the number of blocks to simulate a reorg, or None for a real reorg.
        This is passed in as self.reorg_count may change asynchronously.
        '''
        if count < 0:
            logger.info('chain reorg detected')
        else:
            logger.info(f'faking a reorg of {count:,d} blocks')
        await self.flush(True)
        
        # CRITICAL FIX: Ensure all caches are cleared before backup_block()
        # backup_block() calls assert_flushed() which expects empty caches
        # If flush() had early return due to empty headers, caches may still have data
        # Clear them explicitly to prevent AssertionError
        cache_counts_before = {
            'headers': len(self.headers),
            'utxo_cache': len(self.utxo_cache),
            'tx_hashes': len(self.tx_hashes)
        }
        self.headers.clear()
        self.tx_hashes.clear()
        self.utxo_cache.clear()
        self.utxo_deletes.clear()
        self.utxo_undos.clear()
        self.new_asset_ids.clear()
        self.new_asset_ids_undos.clear()
        self.asset_ids_deletes.clear()
        self.new_h160_ids.clear()
        self.new_h160_ids_undos.clear()
        self.h160_ids_deletes.clear()
        self.asset_metadata.clear()
        self.asset_metadata_undos.clear()
        self.asset_metadata_deletes.clear()
        self.asset_metadata_history.clear()
        self.asset_metadata_history_undos.clear()
        self.asset_metadata_history_deletes.clear()
        self.asset_broadcasts.clear()
        self.asset_broadcasts_deletes.clear()
        self.tags.clear()
        self.tags_undos.clear()
        self.tags_deletes.clear()
        self.tag_history.clear()
        self.tag_history_undos.clear()
        self.tag_history_deletes.clear()
        self.freezes.clear()
        self.freezes_undos.clear()
        self.freezes_deletes.clear()
        self.freeze_history.clear()
        self.freeze_history_undos.clear()
        self.freeze_history_deletes.clear()
        self.verifiers.clear()
        self.verifiers_undos.clear()
        self.verifiers_deletes.clear()
        self.verifier_history.clear()
        self.verifier_history_undos.clear()
        self.verifier_history_deletes.clear()
        self.associations.clear()
        self.associations_undos.clear()
        self.associations_deletes.clear()
        self.association_history.clear()
        self.association_history_undos.clear()
        self.association_history_deletes.clear()
        self.touched.clear()
        self.asset_touched.clear()
        self.qualifier_touched.clear()
        self.h160_touched.clear()
        self.broadcast_touched.clear()
        self.frozen_touched.clear()
        self.validator_touched.clear()
        self.qualifier_association_touched.clear()
        
        # DEBUG: Log cache clearing if there was data (helps diagnose reorg issues)
        if any(cache_counts_before.values()):
            logger.debug(f'Reorg cache cleanup: cleared {cache_counts_before} before backup')

        start, hex_hashes = await self._reorg_hashes(count)
        pairs = reversed(list(enumerate(hex_hashes, start=start)))
        await OnDiskBlock.prefetch_many(self.daemon, pairs, 'reorg')

        for hex_hash in reversed(hex_hashes):
            if hex_hash != hash_to_hex_str(self.state.tip):
                logger.error(f'block {hex_hash} is not tip; cannot back up')
                return
            block = await OnDiskBlock.streamed_block(self.coin, hex_hash)
            if not block:
                break
            await self.run_with_lock(run_in_thread(self.backup_block, block))       
        
        logger.info(f'backed up to height {self.state.height:,d}')
        self.backed_up_event.set()
        self.backed_up_event.clear()

    async def _reorg_hashes(self, count):
        '''Return a pair (start, hashes) of blocks to back up during a
        reorg.

        The hashes are returned in order of increasing height.  Start
        is the height of the first hash, last of the last.
        '''
        start, count = await self._calc_reorg_range(count)
        last = start + count - 1
        
        if count == 1:
            logger.info(f'chain was reorganised replacing 1 block at height {start:,d}')
        else:
            logger.info(f'chain was reorganised replacing {count:,d} blocks at heights '
                        f'{start:,d}-{last:,d}')

        hashes = await self.db.fs_block_hashes(start, count)
        hex_hashes = [hash_to_hex_str(block_hash) for block_hash in hashes]
        return start, hex_hashes

    async def _calc_reorg_range(self, count):
        '''Calculate the reorg range'''

        def diff_pos(hashes1, hashes2):
            '''Returns the index of the first difference in the hash lists.
            If both lists match returns their length.'''
            for n, (hash1, hash2) in enumerate(zip(hashes1, hashes2)):
                if hash1 != hash2:
                    return n
            return len(hashes)

        height = self.state.height
        if count < 0:
            # A real reorg
            start = height - 1
            count = 1
            while start > 0:
                hashes = await self.db.fs_block_hashes(start, count)
                hex_hashes = [hash_to_hex_str(hash) for hash in hashes]
                d_hex_hashes = await self.daemon.block_hex_hashes(start, count)
                n = diff_pos(hex_hashes, d_hex_hashes)
                if n > 0:
                    start += n
                    break
                count = min(count * 2, start)
                start -= count

            count = (height - start) + 1
        else:
            start = (height - count) + 1

        return start, count

    # - Flushing
    def flush_data(self):
        '''The data for a flush.'''        
        return FlushData(self.state, self.headers, self.tx_hashes,
                         self.utxo_undos, self.utxo_cache, self.utxo_deletes,
                         self.new_asset_ids, self.new_asset_ids_undos, self.asset_ids_deletes,
                         self.new_h160_ids, self.new_h160_ids_undos, self.h160_ids_deletes,
                         self.asset_metadata, self.asset_metadata_undos, self.asset_metadata_deletes, 
                         self.asset_metadata_history, self.asset_metadata_history_undos, self.asset_metadata_history_deletes,
                         self.asset_broadcasts, self.asset_broadcasts_deletes, self.asset_broadcasts_deletes,
                         self.tags, self.tags_undos, self.tags_deletes,
                         self.tag_history, self.tag_history_undos, self.tag_history_deletes,
                         self.freezes, self.freezes_undos, self.freezes_deletes,
                         self.freeze_history, self.freeze_history_undos, self.freeze_history_deletes,
                         self.verifiers, self.verifiers_undos, self.verifiers_deletes,
                         self.verifier_history, self.verifier_history_undos, self.verifier_history_deletes,
                         self.associations, self.associations_undos, self.associations_deletes,
                         self.association_history, self.association_history_undos, self.association_history_deletes
        )
    async def flush(self, flush_utxos):
        self.force_flush_arg = None
        # Skip flush if no new blocks (prevents double flush)
        if not self.headers:
            return
        # Estimate size remaining
        daemon_height = self.daemon.cached_height()
        tail_blocks = max(0, (daemon_height - max(self.state.height, self.coin.CHAIN_SIZE_HEIGHT)))
        size_remaining = (max(self.coin.CHAIN_SIZE - self.state.chain_size, 0) +
                          tail_blocks * self.coin.AVG_BLOCK_SIZE)
        await run_in_thread(self.db.flush_dbs, self.flush_data(), flush_utxos, size_remaining)

    async def check_cache_size_loop(self):
        '''Signal to flush caches if they get too big.'''
        # Good average estimates based on traversal of subobjects and
        # requesting size from Python (see deep_getsizeof).

        one_MB = 1000 * 1000
        cache_MB = self.env.cache_MB
        OnDiskBlock.daemon = self.daemon

        while True:
            utxo_cache_size = len(self.utxo_cache) * 213
            db_deletes_size = len(self.utxo_deletes) * 65
            hist_cache_size = self.db.history.unflushed_memsize()
            # Roughly ntxs * 32 + nblocks * 42
            tx_hash_size = ((self.state.tx_count - self.db.fs_tx_count) * 32
                            + (self.state.height - self.db.fs_height) * 42)

            # https://github.com/kyuupichan/electrumx/blob/281d9dacef5f90c62867d482e252bf4a26e17fbd/server/block_processor.py#L673
            # seeming uses 1000 entries to get the ratio
            # undo infos are not set when syncing
            # we do not use asset deletes unless roll back happens -- neglect this
            # worst cases are used for asset id, metadata, and verifier

            asset_id_size = len(self.new_asset_ids) * 182
            h160_id_size = len(self.new_h160_ids) * 167
            metadata_size = len(self.asset_metadata) * 237
            metadata_history_size = len(self.asset_metadata_history) * 208
            broadcast_size = len(self.asset_broadcasts) * 207
            tag_size = len(self.tags) * 158
            tag_history_size = len(self.tag_history) * 159
            freeze_size = len(self.freezes) * 153
            freeze_history_size = len(self.freeze_history) * 110
            verifier_size = len(self.verifiers) * 158
            verifier_history_size = len(self.verifier_history) * 257
            association_size = len(self.associations) * 163
            association_history_size = len(self.association_history) * 120

            utxo_MB = (db_deletes_size + utxo_cache_size) // one_MB
            hist_MB = (hist_cache_size + tx_hash_size) // one_MB
            asset_MB = (
                asset_id_size +
                h160_id_size +
                metadata_size +
                metadata_history_size +
                broadcast_size +
                tag_size +
                tag_history_size +
                freeze_size +
                freeze_history_size +
                verifier_size +
                verifier_history_size +
                association_size +
                association_history_size
            ) // one_MB

            #from electrumx.lib.util import deep_getsizeof

            OnDiskBlock.log_block = True
            if hist_cache_size:
                # Include height information - use current processing height
                # Use self.state.height which is updated immediately after block processing
                our_height = self.state.height
                daemon_height = self.daemon.cached_height()
                logger.info(f'our height: {our_height:,d} daemon: {daemon_height:,d} '
                          f'UTXOs {utxo_MB:,d}MB Assets {asset_MB:,d}MB hist {hist_MB:,d}MB')

            # Flush history if it takes up over 20% of cache memory.
            # Flush UTXOs once they take up 80% of cache memory.
            # When caught up, flush every block to ensure immediate client availability
            blocks_pending = len(self.headers)
            
            cache_full = asset_MB + utxo_MB + hist_MB >= cache_MB
            hist_full = hist_MB >= cache_MB // 5
            blocks_ready = self.caught_up and blocks_pending >= 1
            
            # B.2: Detect lag and force processing
            daemon_height = self.daemon.cached_height()
            blocks_behind = daemon_height - self.state.height
            
            # Force flush if server is lagging behind daemon
            lag_detected = blocks_behind > 1 and self.caught_up
            
            should_flush = cache_full or hist_full or blocks_ready or lag_detected
            
            if lag_detected:
                logger.debug(f'Lag detected: {blocks_behind} blocks behind daemon, forcing flush')
            
            # CRITICAL: Don't interfere with batch processing
            # If advance_blocks() is processing, defer flush request until batch completes
            if self.processing_blocks:
                await sleep(5)
                continue
            
            if should_flush:
                flush_utxos = (utxo_MB + asset_MB) >= cache_MB * 4 // 5
                
                # FIXED: Always use force_flush_arg for consistency
                # Notifications will be handled by advance_and_maybe_flush() via on_block()
                # This prevents duplicate notifications and ensures proper coordination via _maybe_notify()
                self.force_flush_arg = flush_utxos
            
            await sleep(5)

    async def advance_blocks(self, hex_hashes):
        '''Process the blocks passed.  Detects and handles reorgs.'''
        
        async def advance_block_only(block):
            '''Process a single block without flushing.'''
            await run_in_thread(self.advance_block, block)
        
        async def do_flush_and_notify(flush_utxos, reason=""):
            '''Flush and notify clients if caught up.'''
            if self.headers:
                await self.flush(flush_utxos)
                
                # When caught up, notify clients immediately after flush
                if self.caught_up:
                    await self.notifications.on_block(
                        self.touched, self.state.height,
                        self.asset_touched, self.qualifier_touched,
                        self.h160_touched, self.broadcast_touched,
                        self.frozen_touched, self.validator_touched,
                        self.qualifier_association_touched
                    )
                    # Clear touched sets after notification
                    self.touched = set()
                    self.asset_touched = set()
                    self.qualifier_touched = set()
                    self.h160_touched = set()
                    self.broadcast_touched = set()
                    self.frozen_touched = set()
                    self.validator_touched = set()
                    self.qualifier_association_touched = set()
                if reason:
                    logger.debug(f'Flush triggered: {reason}')

        # Set processing flag to prevent check_cache_size_loop() interference
        self.processing_blocks = True
        
        try:
            batch_start_time = time.time()
            batch_size = len(hex_hashes)
            
            if batch_size > 0:
                logger.debug(f'Processing batch of {batch_size} blocks')
            
            # Process ALL blocks first - maximum speed, no waits
            blocks_processed = 0
            previous_block_hash = None
            
            for hex_hash in hex_hashes:
                # Stop if we must flush (reorg detected)
                if self.reorg_count is not None:
                    # CRITICAL: If reorg detected during batch processing, flush any pending blocks first
                    # This ensures undo information is saved before attempting backup
                    if blocks_processed > 0 and self.headers:
                        logger.debug(f'Reorg detected during batch processing, flushing {blocks_processed} processed blocks before backup')
                        flush_reason = "reorg detected - flushing before backup"
                        # CRITICAL FIX: Always flush with flush_utxos=True before reorg
                        # Without undo info, backup_block() will fail
                        flush_utxos = True
                        if self.force_flush_arg is not None:
                            flush_utxos = self.force_flush_arg
                            self.force_flush_arg = None
                        await do_flush_and_notify(flush_utxos, flush_reason)
                    break
                
                block = await OnDiskBlock.streamed_block(self.coin, hex_hash)
                if not block:
                    break
                
                # Validate block ordering - read header directly to check prevhash
                # This is done before advance_block() which uses 'with block:' context
                try:
                    with block:
                        block_header = block.header
                        if block_header is None:
                            # Header not parsed yet, skip validation
                            pass
                        else:
                            if previous_block_hash is not None:
                                expected_prev_hash = self.coin.header_prevhash(block_header)
                                if previous_block_hash != expected_prev_hash:
                                    logger.warning(f'Block ordering issue: block {block.height} expected prevhash {hash_to_hex_str(expected_prev_hash)}, '
                                                 f'but previous block hash was {hash_to_hex_str(previous_block_hash)}')
                            elif blocks_processed == 0:
                                # First block in batch - validate it connects to current tip
                                expected_prev_hash = self.coin.header_prevhash(block_header)
                                if self.state.tip != expected_prev_hash:
                                    logger.warning(f'Block ordering issue: first block {block.height} expected prevhash {hash_to_hex_str(expected_prev_hash)}, '
                                                 f'but current tip is {hash_to_hex_str(self.state.tip)}')
                            
                            # Store hash for next iteration validation
                            previous_block_hash = self.coin.header_hash(block_header)
                except Exception as e:
                    # If validation fails, log but continue processing
                    logger.debug(f'Could not validate block ordering for {hex_hash}: {e}')
                
                # Process block without flushing immediately
                # advance_block() will use 'with block:' context internally
                await self.run_with_lock(advance_block_only(block))
                blocks_processed += 1
            
            # Calculate processing time
            processing_time = time.time() - batch_start_time
            
            # CRITICAL: After processing ALL blocks, flush IMMEDIATELY (no delay)
            # Check force_flush_arg only once after all blocks processed
            # Skip flush if reorg was detected (already flushed above)
            if blocks_processed > 0 and self.reorg_count is None:
                flush_reason = ""
                flush_utxos = False
                
                if self.force_flush_arg is not None:
                    # Cache/history full - flush with UTXO flush if needed
                    flush_utxos = self.force_flush_arg
                    flush_reason = "cache/history full"
                    self.force_flush_arg = None
                elif self.caught_up and self.headers:
                    # CRITICAL FIX: Always flush with flush_utxos=True when caught up
                    # This ensures undo information is saved for every block
                    # Without undo info, reorgs will fail with "no undo information found"
                    # Undo info is small (~50KB/block) and critical for reorg handling
                    flush_utxos = True
                    flush_reason = "batch complete"
                
                if self.caught_up and self.headers:
                    # Flush immediately - clients receive updates without delay
                    logger.debug(f'Processed {blocks_processed} blocks in {processing_time:.2f}s, flushing immediately')
                    await do_flush_and_notify(flush_utxos, flush_reason)
            
            # If we've not caught up we have no clients for the touched set
            if not self.caught_up:
                self.touched = set()
                self.asset_touched = set()
                self.qualifier_touched = set()
                self.h160_touched = set()
                self.broadcast_touched = set()
                self.frozen_touched = set()
                self.validator_touched = set()
                self.qualifier_association_touched = set()
        
        finally:
            # Always clear processing flag, even if exception occurs
            self.processing_blocks = False
        

    def advance_block(self, block: OnDiskBlock):
        '''Advance once block.  It is already verified they correctly connect onto our tip.'''
        is_unspendable = (is_unspendable_genesis if block.height >= self.coin.GENESIS_ACTIVATION
                          else is_unspendable_legacy)

        # Use local vars for speed in the loops
        state = self.state
        tx_hashes = []
        
        internal_utxo_undo_info = []
        internal_asset_id_undo_info = []
        internal_h160_id_undo_info = []
        internal_metadata_undo_info = []
        internal_metadata_history_undo_info = []
        internal_broadcast_undo_info = []
        internal_tag_undo_info = []
        internal_tag_history_undo_info = []
        internal_freeze_undo_info = []
        internal_freeze_history_undo_info = []
        internal_verifier_undo_info = []
        internal_verifier_history_undo_info = []
        internal_association_undo_info = []
        internal_association_history_undo_info = []
        
        tx_num: int = state.tx_count
        asset_num: int = state.asset_count
        h160_num: int = state.h160_count
        script_hashX = self.coin.hashX_from_script

        put_utxo = self.utxo_cache.__setitem__
        put_asset_id = self.new_asset_ids.__setitem__
        put_h160_id = self.new_h160_ids.__setitem__
        put_metadata = self.asset_metadata.__setitem__
        put_metadata_history = self.asset_metadata_history.__setitem__
        put_broadcast = self.asset_broadcasts.__setitem__
        put_tag = self.tags.__setitem__
        put_tag_history = self.tag_history.__setitem__
        put_freeze = self.freezes.__setitem__
        put_freeze_history = self.freeze_history.__setitem__
        put_verifier = self.verifiers.__setitem__
        put_verifier_history = self.verifier_history.__setitem__
        put_association = self.associations.__setitem__
        put_association_history = self.association_history.__setitem__
        
        def lookup_or_add_asset_id(asset: bytes, assert_created=True) -> bytes:
            nonlocal asset_num
            idb = self.new_asset_ids.get(asset, None)
            if idb is not None:
                return idb
            idb = self.db.get_id_for_asset(asset)
            if idb is not None:
                return idb
            if assert_created:
                raise ChainError(f'{asset} should already be created.')
            idb = pack_le_uint32(asset_num)
            put_asset_id(asset, idb)
            internal_asset_id_undo_info.append(idb)
            asset_num += 1
            assert asset_num < int.from_bytes(NULL_U32, 'little'), 'max asset id reached'
            return idb
        
        def lookup_or_add_h160_id(h160: bytes) -> bytes:
            nonlocal h160_num
            idb = self.new_h160_ids.get(h160, None)
            if idb is not None:
                return idb
            idb = self.db.get_id_for_h160(h160)
            if idb is not None:
                return idb
            idb = pack_le_uint32(h160_num)
            put_h160_id(h160, idb)
            internal_h160_id_undo_info.append(idb)
            h160_num += 1
            assert asset_num <= int.from_bytes(NULL_U32, 'little'), 'max h160 id reached'
            return idb

        spend_utxo = self.spend_utxo
        
        update_hashX_touched = self.touched.update
        add_asset_touched = self.asset_touched.add
        add_qualifier_touched = self.qualifier_touched.add
        add_h160_touched = self.h160_touched.add
        add_broadcast_touched = self.broadcast_touched.add
        add_freeze_touched = self.frozen_touched.add
        add_verifier_touched = self.validator_touched.add
        add_association_touched = self.qualifier_association_touched.add
        
        hashXs_by_tx = []
        append_hashXs = hashXs_by_tx.append
        append_tx_hash = tx_hashes.append
        to_le_uint32 = pack_le_uint32
        to_le_uint64 = pack_le_uint64
        utxo_count_delta = 0

        with block as raw_block:
            # Header is already correctly parsed in __enter__ for both MeowPow and AuxPOW blocks
            # No need to re-read the entire block - just validate prevhash and iterate transactions
            
            if self.coin.header_prevhash(block.header) != self.state.tip:
                self.reorg_count = -1
                return
            
            self.ok = False
            # iter_txs() reads transactions from current file cursor (after header)
            # This avoids re-reading the entire block that was already read in __enter__
            for tx, tx_hash in block.iter_txs():
                hashXs = []
                inputHashXs = defaultdict(set)
                append_hashX = hashXs.append
                tx_numb = to_le_uint64(tx_num)[:5]
                current_restricted_asset = None
                current_qualifiers = []
                current_verifier_string = None
                qualifiers_idx = None
                restricted_idx = None
                # Spend the inputs
                for txin in tx.inputs:
                    if txin.is_generation():  # Don't spend block rewards
                        continue
                    utxo_count_delta -= 1
                    cache_value = spend_utxo(bytes(txin.prev_hash), txin.prev_idx)
                    internal_utxo_undo_info.append(cache_value)
                    hashX = cache_value[:-17]
                    asset_id = cache_value[-4:]
                    assert len(hashX) == HASHX_LEN
                    append_hashX(hashX)
                    inputHashXs[hashX].add(asset_id)

                # Add the new UTXOs
                for idx, txout in enumerate(tx.outputs):
                    # Ignore unspendable outputs
                    if is_unspendable(txout.pk_script):
                        continue
                    utxo_count_delta += 1

                    # Many scripts are malformed. This is very problematic...
                    # We cannot assume scripts are valid just because they are from a node
                    # We need to check for:
                    # Bitcoin PUSHOPs
                    # Standard VARINTs
                    # Just anything really

                    if len(txout.pk_script) == 0:
                        hashX = script_hashX(txout.pk_script)
                        append_hashX(hashX)
                        put_utxo(tx_hash + to_le_uint32(idx),
                            hashX + tx_numb + to_le_uint64(txout.value) + NULL_U32)
                        continue

                    # deserialize the script pubkey
                    ops = Script.get_ops(txout.pk_script)

                    if ops[0][0] == -1:
                        # Quick check for invalid script.
                        # Hash as-is for possible spends and continue.
                        hashX = script_hashX(txout.pk_script)
                        append_hashX(hashX)
                        put_utxo(tx_hash + to_le_uint32(idx),
                                hashX + tx_numb + to_le_uint64(txout.value) + NULL_U32)
                        if self.env.write_bad_vouts_to_file:
                            b = bytearray(tx_hash)
                            b.reverse()
                            file_name = base_encode(hashlib.md5(tx_hash + txout.pk_script).digest(), 58)
                            with open(os.path.join(self.bad_vouts_path, str(block.height) + '_BADOPS_' + file_name),
                                    'w') as f:
                                f.write('TXID : {}\n'.format(b.hex()))
                                f.write('SCRIPT : {}\n'.format(txout.pk_script.hex()))
                                f.write('OPS : {}\n'.format(repr(ops)))
                        continue

                    invalid_script = False
                    op_ptr = -1
                    for i in range(len(ops)):
                        op = ops[i][0]  # The OpCode
                        if op == OpCodes.OP_MEWC_ASSET:
                            op_ptr = i
                            break
                        if op == -1:
                            invalid_script = True
                            break

                    if invalid_script:
                        # This script could not be parsed properly before any OP_MEWC_ASSETs.
                        # Hash as-is for possible spends and continue.
                        hashX = script_hashX(txout.pk_script)
                        append_hashX(hashX)
                        put_utxo(tx_hash + to_le_uint32(idx),
                                hashX + tx_numb + to_le_uint64(txout.value) + NULL_U32)
                        if self.env.write_bad_vouts_to_file:
                            b = bytearray(tx_hash)
                            b.reverse()
                            file_name = base_encode(hashlib.md5(tx_hash + txout.pk_script).digest(), 58)
                            with open(os.path.join(self.bad_vouts_path, str(block.height) + '_BADOPS_' + file_name),
                                    'w') as f:
                                f.write('TXID : {}\n'.format(b.hex()))
                                f.write('SCRIPT : {}\n'.format(txout.pk_script.hex()))
                                f.write('OPS : {}\n'.format(str(ops)))
                        continue

                    if op_ptr == 0:
                        # This is an asset tag
                        # continue is called after this block

                        idx = to_le_uint32(idx)

                        try:
                            if match_script_against_template(ops, ASSET_NULL_TEMPLATE) > -1:
                                # This is what tags an address with a qualifier
                                h160_shared = ops[1][2]
                                h160 = bytes(h160_shared)
                                asset_portion = ops[2][2]
                                asset_portion_deserializer = DataParser(asset_portion)
                                name_byte_len, asset_name = asset_portion_deserializer.read_var_bytes_tuple_bytes()
                                flag = asset_portion_deserializer.read_byte()

                                asset_id = lookup_or_add_asset_id(asset_name, False)
                                h160_id = lookup_or_add_h160_id(h160)

                                current_latest_tag = self.tags.get(asset_id + h160_id, None)
                                if current_latest_tag is None:
                                    current_latest_tag = self.db.asset_db.get(PREFIX_ASSET_TAG_CURRENT + asset_id + h160_id)
                                if current_latest_tag is None:
                                    current_latest_tag = b'\xff' * (4 + 5)
                                internal_tag_undo_info.append(asset_id + h160_id + current_latest_tag)
                                put_tag(asset_id + h160_id, idx + tx_numb)

                                put_tag_history(asset_id + h160_id + idx + tx_numb, flag)
                                internal_tag_history_undo_info.append(asset_id + h160_id + idx + tx_numb)

                                add_qualifier_touched(asset_name.decode())
                                add_h160_touched(h160)
                            elif match_script_against_template(ops, ASSET_NULL_VERIFIER_TEMPLATE) > -1:
                                # This associates a restricted asset with qualifier tags in a boolean logic string
                                qualifiers_b = ops[2][2]
                                qualifiers_deserializer = DataParser(qualifiers_b)
                                asset_names = qualifiers_deserializer.read_var_bytes_as_ascii()
                                current_verifier_string = asset_names
                                current_qualifiers = re.findall(r'([A-Z0-9_.]+)', asset_names)
                                qualifiers_idx = idx
                            elif match_script_against_template(ops, ASSET_GLOBAL_RESTRICTION_TEMPLATE) > -1:
                                # This globally freezes a restricted asset
                                asset_portion = ops[3][2]

                                asset_portion_deserializer = DataParser(asset_portion)
                                asset_name_len, asset_name = asset_portion_deserializer.read_var_bytes_tuple_bytes()
                                flag = asset_portion_deserializer.read_byte()

                                asset_id = lookup_or_add_asset_id(asset_name, False)
                                current_latest_freeze = self.tags.get(asset_id, None)
                                if current_latest_freeze is None:
                                    current_latest_freeze = self.db.asset_db.get(PREFIX_FREEZE_CURRENT + asset_id, None)
                                if current_latest_freeze is None:
                                    current_latest_freeze = b'\xff' * (4 + 5)                                 
                                internal_freeze_undo_info.append(asset_id + current_latest_freeze)
                                put_freeze(asset_id, idx + tx_numb)

                                put_freeze_history(asset_id + idx + tx_numb, flag)
                                internal_freeze_history_undo_info.append(asset_id + idx + tx_numb)

                                add_freeze_touched(asset_name.decode())
                            else:
                                raise Exception('Bad null asset script ops')
                        except Exception as e:
                            if self.env.write_bad_vouts_to_file:
                                b = bytearray(tx_hash)
                                b.reverse()
                                file_name = base_encode(hashlib.md5(tx_hash + txout.pk_script).digest(), 58)
                                with open(os.path.join(self.bad_vouts_path,
                                                    str(block.height) + '_NULLASSET_' + file_name), 'w') as f:
                                    f.write('TXID : {}\n'.format(b.hex()))
                                    f.write('SCRIPT : {}\n'.format(txout.pk_script.hex()))
                                    f.write('OpCodes : {}\n'.format(repr(ops)))
                                    f.write('Exception : {}\n'.format(repr(e)))
                                    f.write('Traceback : {}\n'.format(traceback.format_exc()))
                            if isinstance(e, (DataParser.ParserException, KeyError)):
                                raise e

                        # Get the hashx and continue
                        hashX = script_hashX(txout.pk_script)
                        append_hashX(hashX)
                        put_utxo(tx_hash + idx,
                            hashX + tx_numb + to_le_uint64(txout.value) + NULL_U32)
                        
                        continue

                    if op_ptr > 0:
                        # This script has OP_MEWC_ASSET. Use everything before this for the script hash.
                        # Get the raw script bytes ending ptr from the previous opcode.
                        script_hash_end = ops[op_ptr - 1][1]
                        hashX = script_hashX(txout.pk_script[:script_hash_end])
                    else:
                        # There is no OP_MEWC_ASSET. Hash as-is.
                        hashX = script_hashX(txout.pk_script)

                    # Now try and add asset info
                    def try_parse_asset(asset_deserializer: DataParser, second_loop=False):
                        nonlocal current_restricted_asset, restricted_idx
                        op = asset_deserializer.read_bytes(3)
                        if op != b'rvn':
                            raise Exception("Expected {}, was {}".format(b'rvn', op))
                        script_type = asset_deserializer.read_byte()
                        asset_name_len, asset_name = asset_deserializer.read_var_bytes_tuple_bytes()
                        idx_b = to_le_uint32(idx)
                        if asset_name[0] == b'$'[0]:
                            current_restricted_asset = asset_name
                            restricted_idx = idx_b
                        if script_type == b'o':
                            # This is an ownership asset. It does not have any metadata.
                            # Just assign it with a value of 1
                            asset_id = lookup_or_add_asset_id(asset_name, False)
                            sats = to_le_uint64(100_000_000)

                            append_hashX(hashX)
                            put_utxo(tx_hash + idx_b,
                                    hashX + tx_numb + sats +
                                    asset_id)

                            put_metadata(asset_id, sats + b'\0\0\0' + idx_b + tx_numb)
                            internal_metadata_undo_info.append(asset_id + b'\0')
                            
                            put_metadata_history(asset_id + idx_b + tx_numb, sats + b'\0')
                            internal_metadata_history_undo_info.append(asset_id + idx_b + tx_numb)

                            add_asset_touched(asset_name.decode('ascii'))
                        else:  # Not an owner asset; has a sat amount
                            sats = asset_deserializer.read_bytes(8)
                            if script_type == b'q':  # A new asset issuance
                                divisions = asset_deserializer.read_byte()
                                reissuable = asset_deserializer.read_byte()
                                has_associated_data = asset_deserializer.read_byte()
                                associated_data = None
                                if has_associated_data != b'\0':
                                    associated_data = asset_deserializer.read_bytes(34)

                                asset_id = lookup_or_add_asset_id(asset_name, False)

                                append_hashX(hashX)
                                put_utxo(tx_hash + idx_b,
                                        hashX + tx_numb + sats +
                                        asset_id)
                                
                                put_metadata(asset_id, sats + divisions + reissuable + has_associated_data + (associated_data or b'') + idx_b + tx_numb)
                                internal_metadata_undo_info.append(asset_id + b'\0')

                                put_metadata_history(asset_id + idx_b + tx_numb, sats + divisions + (associated_data or b''))
                                internal_metadata_history_undo_info.append(asset_id + idx_b + tx_numb)

                                add_asset_touched(asset_name.decode('ascii'))
                            elif script_type == b'r':  # An asset re-issuance
                                divisions = this_divisions = asset_deserializer.read_byte()
                                reissuable = asset_deserializer.read_byte()

                                asset_id = lookup_or_add_asset_id(asset_name)

                                current_metadata = self.asset_metadata.get(asset_id, None)
                                if current_metadata is None:
                                    current_metadata = self.db.asset_db.get(PREFIX_METADATA + asset_id)
                                assert current_metadata

                                old_data_parser = DataParser(current_metadata)
                                old_sats, = unpack_le_uint64(old_data_parser.read_bytes(8))
                                new_sats, = unpack_le_uint64(sats)

                                # How many outpoints we need to save
                                use_old_div = False
                                use_old_ipfs = False

                                total_sats = old_sats + new_sats

                                old_divisions = old_data_parser.read_byte()
                                if divisions == b'\xff':  # Unchanged division amount
                                    use_old_div = True
                                    divisions = old_divisions
                                
                                _old_reissue = old_data_parser.read_boolean()
                                if not _old_reissue:
                                    raise ValueError('We are reissuing a non-reissuable asset!')

                                if asset_deserializer.is_finished():
                                    ipfs = None
                                else:
                                    if second_loop:
                                        if asset_deserializer.cursor + 34 <= asset_deserializer.length:
                                            ipfs = asset_deserializer.read_bytes(34)
                                        else:
                                            ipfs = None
                                    else:
                                        ipfs = asset_deserializer.read_bytes(34)

                                this_ipfs = ipfs

                                old_boolean = old_data_parser.read_boolean()
                                if old_boolean:
                                    old_ipfs = old_data_parser.read_bytes(34)

                                if not ipfs and old_boolean:
                                    use_old_ipfs = True
                                    ipfs = old_ipfs

                                old_outpoint = old_data_parser.read_bytes(9)
                                old_div_outpoint = None
                                old_ipfs_outpoint = None
                                while not old_data_parser.is_finished():
                                    source_type = old_data_parser.read_int()
                                    if source_type == 0:
                                        old_div_outpoint = old_data_parser.read_bytes(9)
                                    elif source_type == 1:
                                        old_ipfs_outpoint = old_data_parser.read_bytes(9)
                                    else:
                                        raise ValueError(f'bad source type {source_type}')

                                metadata = pack_le_uint64(total_sats) + divisions + reissuable + \
                                    (b'\x01' if ipfs else b'\0') + (ipfs if ipfs else b'') + idx_b + tx_numb + \
                                    ((b'\0' + (old_div_outpoint or old_outpoint)) if use_old_div else b'') + \
                                    ((b'\x01' + (old_ipfs_outpoint or old_outpoint)) if use_old_ipfs else b'')

                                append_hashX(hashX)
                                put_utxo(tx_hash + idx_b,
                                        hashX + tx_numb + sats +
                                        asset_id)
                                
                                put_metadata(asset_id, metadata)
                                
                                # current_metadata is at most 74 bytes
                                internal_metadata_undo_info.append(asset_id + bytes([len(current_metadata)]) + current_metadata)

                                put_metadata_history(asset_id + idx_b + tx_numb, sats + this_divisions + (this_ipfs or b''))
                                internal_metadata_history_undo_info.append(asset_id + idx_b + tx_numb)

                                add_asset_touched(asset_name.decode('ascii'))
                            elif script_type == b't':
                                asset_id = lookup_or_add_asset_id(asset_name)
                                append_hashX(hashX)
                                put_utxo(tx_hash + to_le_uint32(idx),
                                        hashX + tx_numb + sats +
                                        asset_id)

                                if not asset_deserializer.is_finished():
                                    if (b'!' in asset_name or b'~' in asset_name) and asset_id in inputHashXs[hashX]:
                                        if second_loop:
                                            if asset_deserializer.cursor + 34 <= asset_deserializer.length:
                                                data = asset_deserializer.read_bytes(34)
                                                timestamp = None
                                                if asset_deserializer.cursor + 8 <= asset_deserializer.length:
                                                    timestamp = asset_deserializer.read_bytes(8)
                                                # This is a message broadcast
                                                put_broadcast(asset_id + idx_b + tx_numb, data + (timestamp if timestamp else b''))
                                                internal_broadcast_undo_info.append(asset_id + idx_b + tx_numb)
                                                add_broadcast_touched(asset_name.decode())
                                        else:
                                            data = asset_deserializer.read_bytes(34)
                                            timestamp = None
                                            if not asset_deserializer.is_finished():
                                                timestamp = asset_deserializer.read_bytes(8)
                                            # This is a message broadcast
                                            put_broadcast(asset_id + idx_b + tx_numb, data + (timestamp if timestamp else b''))
                                            internal_broadcast_undo_info.append(asset_id + idx_b + tx_numb)
                                            add_broadcast_touched(asset_name.decode())
                            
                            else: 
                                raise Exception('Unknown asset type: {}'.format(script_type))

                    # function for malformed asset
                    def try_parse_asset_iterative(script: bytes):
                        while script[:3] != b'rvn' and len(script) > 0:
                            script = script[1:]
                        assert script[:3] == b'rvn'
                        return try_parse_asset(DataParser(script), True)

                    # Me @ core devs
                    # https://www.youtube.com/watch?v=iZlpsneDGBQ

                    if 0 < op_ptr < len(ops):
                        assert ops[op_ptr][0] == OpCodes.OP_MEWC_ASSET  # Sanity check
                        try:
                            next_op = ops[op_ptr + 1]
                            if next_op[0] == -1:
                                # This contains the raw data. Deserialize.
                                asset_script_deserializer = DataParser(next_op[2])
                                asset_script = asset_script_deserializer.read_var_bytes()
                            elif len(ops) > op_ptr + 4 and \
                                    ops[op_ptr + 2][0] == b'r'[0] and \
                                    ops[op_ptr + 3][0] == b'v'[0] and \
                                    ops[op_ptr + 4][0] == b'n'[0]:
                                asset_script_portion = txout.pk_script[ops[op_ptr][1]:]
                                asset_script_deserializer = DataParser(asset_script_portion)
                                asset_script = asset_script_deserializer.read_var_bytes()
                            else:
                                # Hurray! This i̶s̶ ̶a̶ COULD BE A properly formatted asset script
                                asset_script = next_op[2]

                            asset_deserializer = DataParser(asset_script)
                            try_parse_asset(asset_deserializer)
                        except Exception:
                            try:
                                try_parse_asset_iterative(txout.pk_script[ops[op_ptr][1]:])
                            except Exception as e:
                                if self.env.write_bad_vouts_to_file:
                                    b = bytearray(tx_hash)
                                    b.reverse()
                                    file_name = base_encode(hashlib.md5(tx_hash + txout.pk_script).digest(), 58)
                                    with open(os.path.join(self.bad_vouts_path, str(block.height) + '_' + file_name),
                                            'w') as f:
                                        f.write('TXID : {}\n'.format(b.hex()))
                                        f.write('SCRIPT : {}\n'.format(txout.pk_script.hex()))
                                        f.write('OpCodes : {}\n'.format(repr(ops)))
                                        f.write('Exception : {}\n'.format(repr(e)))
                                        f.write('Traceback : {}\n'.format(traceback.format_exc()))
                                append_hashX(hashX)
                                put_utxo(tx_hash + to_le_uint32(idx),
                                    hashX + tx_numb + to_le_uint64(txout.value) + NULL_U32)
                    else:
                        append_hashX(hashX)
                        put_utxo(tx_hash + to_le_uint32(idx),
                            hashX + tx_numb + to_le_uint64(txout.value) + NULL_U32)


                if current_restricted_asset and current_verifier_string:
                    # Verifier string
                    restricted_asset_id = lookup_or_add_asset_id(current_restricted_asset)

                    previous_verifier_string_existed = True
                    current_latest_verifier = self.verifiers.get(restricted_asset_id, None)
                    if current_latest_verifier is None:
                        current_latest_verifier = self.db.asset_db.get(PREFIX_VERIFIER_CURRENT + restricted_asset_id, None)
                    if current_latest_verifier is None:
                        previous_verifier_string_existed = False
                        current_latest_verifier = b'\xff' * (4 + 4 + 5)                                 
                    internal_verifier_undo_info.append(restricted_asset_id + current_latest_verifier)
                    put_verifier(restricted_asset_id, restricted_idx + qualifiers_idx + tx_numb)

                    put_verifier_history(restricted_asset_id + restricted_idx + qualifiers_idx + tx_numb, current_verifier_string.encode())
                    internal_verifier_history_undo_info.append(restricted_asset_id + restricted_idx + qualifiers_idx + tx_numb)

                    add_verifier_touched(current_restricted_asset.decode())

                    # Qualifier associations
                    if previous_verifier_string_existed:
                        verifier_string_bytes = self.verifier_history.get(restricted_asset_id + current_latest_verifier, None)
                        if verifier_string_bytes is None:
                            verifier_string_bytes = self.db.asset_db.get(PREFIX_VERIFIER_HISTORY + restricted_asset_id + current_latest_verifier, None)
                            assert verifier_string_bytes
                        for qualifier in re.findall(r'([A-Z0-9_.]+)', verifier_string_bytes.decode()):
                            if qualifier not in current_qualifiers:
                                qualifier_id = lookup_or_add_asset_id(f'#{qualifier}'.encode())
                                previous_association = self.associations.get(qualifier_id + restricted_asset_id, None)
                                if previous_association is None:
                                    previous_association = self.db.asset_db.get(PREFIX_ASSOCIATION_CURRENT + qualifier_id + restricted_asset_id, None)
                                    assert previous_association
                                internal_association_undo_info.append(qualifier_id + restricted_asset_id + previous_association)
                                put_association(qualifier_id + restricted_asset_id, restricted_idx + qualifiers_idx + tx_numb)

                                put_association_history(qualifier_id + restricted_asset_id + restricted_idx + qualifiers_idx + tx_numb, b'\0')
                                internal_association_history_undo_info.append(qualifier_id + restricted_asset_id + restricted_idx + qualifiers_idx + tx_numb)

                                add_association_touched(f'#{qualifier}')

                    for qualifier in current_qualifiers:
                        qualifier_id = lookup_or_add_asset_id(f'#{qualifier}'.encode())
                        previous_association = self.associations.get(qualifier_id + restricted_asset_id, None)
                        if previous_association is None:
                            previous_association = self.db.asset_db.get(PREFIX_ASSOCIATION_CURRENT + qualifier_id + restricted_asset_id, None)
                        if previous_association is None:
                            previous_association = b'\xff' * (4 + 4 + 5)
                        internal_association_undo_info.append(qualifier_id + restricted_asset_id + previous_association)
                        put_association(qualifier_id + restricted_asset_id, restricted_idx + qualifiers_idx + tx_numb)

                        put_association_history(qualifier_id + restricted_asset_id + restricted_idx + qualifiers_idx + tx_numb, b'\x01')
                        internal_association_history_undo_info.append(qualifier_id + restricted_asset_id + restricted_idx + qualifiers_idx + tx_numb)

                        add_association_touched(f'#{qualifier}')


                append_hashXs(hashXs)
                update_hashX_touched(hashXs)
                append_tx_hash(tx_hash)
                tx_num += 1

        # Do this first - it uses the prior state
        self.tx_hashes.append(b''.join(tx_hashes))
        self.db.history.add_unflushed(hashXs_by_tx, state.tx_count)
        self.db.tx_counts.append(tx_num)

        if block.height >= self.db.min_undo_height(self.daemon.cached_height()):
            self.utxo_undos.append((internal_utxo_undo_info, block.height))
            self.new_asset_ids_undos.append((internal_asset_id_undo_info, block.height))
            self.new_h160_ids_undos.append((internal_h160_id_undo_info, block.height))
            self.asset_metadata_undos.append((internal_metadata_undo_info, block.height))
            self.asset_metadata_history_undos.append((internal_metadata_history_undo_info, block.height))
            self.asset_broadcasts_undos.append((internal_broadcast_undo_info, block.height))
            self.tags_undos.append((internal_tag_undo_info, block.height))
            self.tag_history_undos.append((internal_tag_history_undo_info, block.height))
            self.freezes_undos.append((internal_freeze_undo_info, block.height))
            self.freeze_history_undos.append((internal_freeze_history_undo_info, block.height))
            self.verifiers_undos.append((internal_verifier_undo_info, block.height))
            self.verifier_history_undos.append((internal_verifier_history_undo_info, block.height))
            self.associations_undos.append((internal_association_undo_info, block.height))
            self.association_history_undos.append((internal_association_history_undo_info, block.height))
        
        # FIXED: Header storage padding logic
        # After KAWPOW_ACTIVATION_HEIGHT (373), all headers in file are stored as 120 bytes
        # - MeowPow blocks: naturally 120 bytes (includes nHeight/nNonce64/mix_hash)
        # - AuxPOW blocks: 80 bytes basic header, padded to 120 for consistent offsets
        # This ensures consistent file offsets regardless of block type
        header_to_store = block.header
        if block.height >= self.coin.KAWPOW_ACTIVATION_HEIGHT:
            if self.coin.is_auxpow_active(block.height) and len(block.header) == 80:
                # AuxPOW header (80 bytes) - pad to 120 for consistent disk storage
                header_to_store = block.header + bytes(40)
            # MeowPow headers are already 120 bytes, no padding needed
        # Pre-KAWPOW headers are stored as-is (80 bytes)
        self.headers.append(header_to_store)
        
        #Update State
        state.height = block.height
        state.tip = self.coin.header_hash(block.header)
        state.chain_size += block.size
        state.utxo_count += utxo_count_delta
        assert tx_num < int.from_bytes(NULL_TXNUMB, 'little'), 'tx num overrun'
        state.tx_count = tx_num
        state.h160_count = h160_num
        state.asset_count = asset_num 
        self.ok = True

    def undo_asset_db(self, height: int):
        assert height > 0

        assets_touched = set()
        data_parser = DataParser(self.db.read_metadata_undo_info(height))
        while not data_parser.is_finished():
            asset_id = data_parser.read_bytes(4)
            asset = self.db.get_asset_for_id(asset_id)
            assert asset
            assets_touched.add(asset.decode())
            metadata = data_parser.read_var_bytes()
            if not metadata:
                self.asset_metadata_deletes.append(PREFIX_METADATA + asset_id)
            else:
                self.asset_metadata[asset_id] = metadata
        data_parser = DataParser(self.db.read_metadata_history_undo_info(height))
        while not data_parser.is_finished():
            key = data_parser.read_bytes(4 + 4 + 5)
            self.asset_metadata_history_deletes.append(PREFIX_METADATA_HISTORY + key)

        broadcasts_touched = set()
        data_parser = DataParser(self.db.read_broadcast_undo_info(height))
        while not data_parser.is_finished():
            asset_id = data_parser.read_bytes(4)
            asset = self.db.get_asset_for_id(asset_id)
            assert asset
            broadcasts_touched.add(asset.decode())
            suffix = data_parser.read_bytes(4 + 5)
            self.asset_broadcasts_deletes.append(PREFIX_BROADCAST + asset + suffix)

        freezes_touched = set()
        data_parser = DataParser(self.db.read_freeze_undo_info(height))
        while not data_parser.is_finished():
            asset_id = data_parser.read_bytes(4)
            idx = data_parser.read_bytes(4)
            tx_num = data_parser.read_bytes(5)

            asset = self.db.get_asset_for_id(asset_id)
            assert asset
            freezes_touched.add(asset.decode())

            if idx == NULL_U32 and tx_num == NULL_TXNUMB:
                self.freezes_deletes.append(PREFIX_FREEZE_CURRENT + asset_id)
            else:
                self.freezes[asset_id] = idx + tx_num
        data_parser = DataParser(self.db.read_freeze_history_undo_info(height))
        while not data_parser.is_finished():
            key = data_parser.read_bytes(4 + 4 + 5)
            self.freeze_history_deletes.append(PREFIX_FREEZE_HISTORY + key)

        h160s_touched = set()
        qualifiers_touched = set()
        data_parser = DataParser(self.db.read_tag_undo_info(height))
        while not data_parser.is_finished():
            asset_id = data_parser.read_bytes(4)
            h160_id = data_parser.read_bytes(4)

            asset = self.db.get_asset_for_id(asset_id)
            assert asset

            h160 = self.db.get_h160_for_id(h160_id)
            assert h160

            qualifiers_touched.add(asset.decode())
            h160s_touched.add(h160)            

            idx = data_parser.read_bytes(4)
            tx_num = data_parser.read_bytes(5)
            if idx == NULL_U32 and tx_num == NULL_TXNUMB:
                self.tags_deletes.append(PREFIX_ASSET_TAG_CURRENT + asset_id + h160_id)
                self.tags_deletes.append(PREFIX_H160_TAG_CURRENT + h160_id + asset_id)
            else:
                self.tags[asset_id + h160_id] = idx + tx_num
        data_parser = DataParser(self.db.read_tag_history_undo_info(height))
        while not data_parser.is_finished():
            asset_id = data_parser.read_bytes(4)
            h160_id = data_parser.read_bytes(4)
            suffix = data_parser.read_bytes(4 + 5)
            self.tag_history_deletes.append(PREFIX_ASSET_TAG_HISTORY + asset_id + suffix)
            self.tag_history_deletes.append(PREFIX_H160_TAG_HISTORY + h160_id + suffix)

        verifiers_touched = set()
        data_parser = DataParser(self.db.read_verifier_undo_info(height))
        while not data_parser.is_finished():
            asset_id = data_parser.read_bytes(4)

            asset = self.db.get_asset_for_id(asset_id)
            assert asset

            verifiers_touched.add(asset.decode())

            restricted_idx = data_parser.read_bytes(4)
            qualifiers_idx = data_parser.read_bytes(4)
            tx_numb = data_parser.read_bytes(5)
            if restricted_idx == NULL_U32 and qualifiers_idx == NULL_U32 and tx_numb == NULL_TXNUMB:
                self.verifiers_deletes.append(PREFIX_VERIFIER_CURRENT + asset_id)
            else:
                self.verifiers[asset_id] = restricted_idx + qualifiers_idx + tx_numb
        data_parser = DataParser(self.db.read_verifier_history_undo_info(height))
        while not data_parser.is_finished():
            key = data_parser.read_bytes(4 + 4 + 4 + 5)
            self.verifier_history_deletes.append(PREFIX_VERIFIER_HISTORY + key)

        associations_touched = set()
        data_parser = DataParser(self.db.read_association_undo_info(height))
        while not data_parser.is_finished():
            qualifier_id = data_parser.read_bytes(4)

            qualifier = self.db.get_asset_for_id(qualifier_id)
            assert qualifier

            associations_touched.add(qualifier.decode())

            restricted_id = data_parser.read_bytes(4)
            restricted_idx = data_parser.read_bytes(4)
            qualifiers_idx = data_parser.read_bytes(4)
            tx_numb = data_parser.read_bytes(5)
            if restricted_idx == NULL_U32 and qualifiers_idx == NULL_U32 and tx_numb == NULL_TXNUMB:
                self.associations_deletes.append(PREFIX_ASSOCIATION_CURRENT + qualifier_id + restricted_id)
            else:
                self.associations[qualifier_id + restricted_id] = restricted_idx + qualifiers_idx + tx_numb
        data_parser = DataParser(self.db.read_association_history_undo_info(height))
        while not data_parser.is_finished():
            key = data_parser.read_bytes(4 + 4 + 4 + 4 + 5)
            self.association_history_deletes.append(PREFIX_ASSOCIATION_HISTORY + key)

        self.asset_touched.update(assets_touched)
        self.h160_touched.update(h160s_touched)
        self.frozen_touched.update(freezes_touched)
        self.broadcast_touched.update(broadcasts_touched)
        self.qualifier_touched.update(qualifiers_touched)
        self.validator_touched.update(verifiers_touched)
        self.qualifier_association_touched.update(qualifiers_touched)

    def backup_block(self, block):
        '''Backup the streamed block.'''
        self.db.assert_flushed(self.flush_data())
        assert block.height > 0
        genesis_activation = self.coin.GENESIS_ACTIVATION
        
        is_unspendable = (is_unspendable_genesis if block.height >= genesis_activation
                          else is_unspendable_legacy)
        
        # CRITICAL: Retry reading undo info with delay if not available immediately
        # This handles race condition where flush completed but commit hasn't finished
        undo_info = None
        max_retries = 5
        retry_delay = 0.1  # 100ms
        
        for attempt in range(max_retries):
            undo_info = self.db.read_utxo_undo_info(block.height)
            if undo_info is not None:
                break
            if attempt < max_retries - 1:
                # Wait before retrying (flush commit may still be in progress)
                time.sleep(retry_delay)
                logger.debug(f'Undo info not available for height {block.height:,d}, retrying ({attempt + 1}/{max_retries})')
            else:
                # Check if block is below min_undo_height (undo info not saved)
                daemon_height = self.daemon.cached_height()
                min_undo = self.db.min_undo_height(daemon_height)
                if block.height < min_undo:
                    raise ChainError(f'no undo information found for height {block.height:,d} '
                                   f'(below min_undo_height {min_undo:,d})')
                else:
                    raise ChainError(f'no undo information found for height {block.height:,d} '
                                   f'after {max_retries} retries (flush may have failed)')

        n = len(undo_info)

        # Use local vars for speed in the loops
        put_utxo = self.utxo_cache.__setitem__
        spend_utxo = self.spend_utxo
        touched_add = self.touched.add
        undo_entry_len = 17 + HASHX_LEN

        # n is our pointer.
        # Items in our list are ordered, but we want them backwards.

        count = 0
        utxo_count_delta = 0
        with block as raw_block:
            # FIXED: Use header already parsed by OnDiskBlock.__enter__()
            # The header is already correctly parsed for both MeowPow and AuxPOW blocks
            # No need to re-parse - just ensure cursor is at correct position for iter_txs_reversed()
            # Reset file cursor to position after header (set by __enter__)
            if raw_block.header_end_offset is not None:
                raw_block.block_file.seek(raw_block.header_end_offset)
            else:
                # Fallback: if header_end_offset not set, use static header length
                header_len = self.coin.static_header_len(raw_block.height)
                raw_block.block_file.seek(header_len)
            
            self.ok = False
            for tx, tx_hash in block.iter_txs_reversed():
                for idx, txout in enumerate(tx.outputs):
                    # Spend the TX outputs.  Be careful with unspendable
                    # outputs - we didn't save those in the first place.
                    if is_unspendable(txout.pk_script):
                        continue
                    utxo_count_delta -= 1
                    cache_value = spend_utxo(tx_hash, idx)
                    touched_add(cache_value[:-17])

                # Restore the inputs
                for txin in reversed(tx.inputs):
                    if txin.is_generation():
                        continue
                    utxo_count_delta += 1
                    n -= undo_entry_len
                    undo_item = undo_info[n:n + undo_entry_len]
                    put_utxo(bytes(txin.prev_hash) + pack_le_uint32(txin.prev_idx), undo_item)
                    touched_add(undo_item[:-17])
                count += 1

        assert n == 0
        
        asset_ids = set()
        assets_touched = set()
        data_parser = DataParser(self.db.read_asset_id_undo_info(block.height))
        while not data_parser.is_finished():
            id_b = data_parser.read_bytes(4)
            asset_b = self.db.suid_db.get(PREFIX_ID_TO_ASSET + id_b)
            asset = asset_b.decode()
            id, = unpack_le_uint32(id_b)
            asset_ids.add(id)
            self.asset_ids_deletes.append(PREFIX_ID_TO_ASSET + id_b)
            self.asset_ids_deletes.append(PREFIX_ASSET_TO_ID + asset_b)
            assets_touched.add(asset)

        h160_ids = set()
        data_parser = DataParser(self.db.read_h160_id_undo_info(block.height))
        while not data_parser.is_finished():
            id_b = data_parser.read_bytes(4)
            h160_b = self.db.suid_db.get(PREFIX_ID_TO_H160 + id_b)
            id, = unpack_le_uint32(id_b)
            h160_ids.add(id)
            self.h160_ids_deletes.append(PREFIX_ID_TO_H160 + id_b)
            self.h160_ids_deletes.append(PREFIX_H160_TO_ID + h160_b)
            
        seen_asset_ids = set()
        min_asset_id = None
        for id in asset_ids:
            assert id not in seen_asset_ids, 'duplicate asset ids'
            seen_asset_ids.add(id)
            if min_asset_id is None or id < min_asset_id:
                min_asset_id = id

        seen_h160_ids = set()
        min_h160_id = None
        for id in h160_ids:
            assert id not in seen_h160_ids, 'duplicate h160 ids'
            seen_h160_ids.add(id)
            if min_h160_id is None or id < min_h160_id:
                min_h160_id = id

        state = self.state
        state.height -= 1
        state.tip = self.coin.header_prevhash(block.header)
        state.chain_size -= block.size
        state.utxo_count += utxo_count_delta
        state.tx_count -= count

        if min_asset_id is None:
            assert len(seen_asset_ids) == 0
        else:
            assert min_asset_id == state.asset_count - len(seen_asset_ids), f'{min_asset_id}, {state.asset_count}, {seen_asset_ids}'
        state.asset_count -= len(seen_asset_ids)

        if min_h160_id is None:
            assert len(seen_h160_ids) == 0
        else:
            assert min_h160_id == state.h160_count - len(seen_h160_ids), f'{min_h160_id}, {state.h160_count}, {seen_h160_ids}'
        state.h160_count -= len(seen_h160_ids)

        self.db.tx_counts.pop()

        # self.touched can include other addresses which is harmless, but remove None.
        self.touched.discard(None)
        self.asset_touched.update(assets_touched)

        # IDs are not yet cleared from db (cleared in flush_backup)
        self.undo_asset_db(block.height)
        self.db.flush_backup(self.flush_data(), self.touched)

        self.ok = True

    '''An in-memory UTXO cache, representing all changes to UTXO state
    since the last DB flush.

    We want to store millions of these in memory for optimal
    performance during initial sync, because then it is possible to
    spend UTXOs without ever going to the database (other than as an
    entry in the address history, and there is only one such entry per
    TX not per UTXO).  So store them in a Python dictionary with
    binary keys and values.

      Key:    TX_HASH + TX_IDX           (32 + 4 = 36 bytes)
      Value:  HASHX + TX_NUM + VALUE     (11 + 5 + 8 = 24 bytes)

    That's 60 bytes of raw data in-memory.  Python dictionary overhead
    means each entry actually uses about 205 bytes of memory.  So
    almost 5 million UTXOs can fit in 1GB of RAM.  There are
    approximately 42 million UTXOs on bitcoin mainnet at height
    433,000.

    Semantics:

      add:   Add it to the cache dictionary.

      spend: Remove it if in the cache dictionary.  Otherwise it's
             been flushed to the DB.  Each UTXO is responsible for two
             entries in the DB.  Mark them for deletion in the next
             cache flush.

    The UTXO database format has to be able to do two things efficiently:

      1.  Given an address be able to list its UTXOs and their values
          so its balance can be efficiently computed.

      2.  When processing transactions, for each prevout spent - a (tx_hash,
          idx) pair - we have to be able to remove it from the DB.  To send
          notifications to clients we also need to know any address it paid
          to.

    To this end we maintain two "tables", one for each point above:

      1.  Key: b'u' + address_hashX + tx_idx + tx_num
          Value: the UTXO value as a 64-bit unsigned integer

      2.  Key: b'h' + compressed_tx_hash + tx_idx + tx_num
          Value: hashX

    The compressed tx hash is just the first few bytes of the hash of
    the tx in which the UTXO was created.  As this is not unique there
    will be potential collisions so tx_num is also in the key.  When
    looking up a UTXO the prefix space of the compressed hash needs to
    be searched and resolved if necessary with the tx_num.  The
    collision rate is low (<0.1%).
    '''

    def spend_utxo(self, tx_hash, tx_idx):
        '''Spend a UTXO and return the 33-byte value.

        If the UTXO is not in the cache it must be on disk.  We store
        all UTXOs so not finding one indicates a logic error or DB
        corruption.
        '''
        # Fast track is it being in the cache
        idx_packed = pack_le_uint32(tx_idx)
        cache_value = self.utxo_cache.pop(tx_hash + idx_packed, None)
        if cache_value:
            return cache_value

        # Spend it from the DB.

        # Key: b'h' + compressed_tx_hash + tx_idx + tx_num
        # Value: hashX
        prefix = PREFIX_UTXO_HISTORY + tx_hash[:4] + idx_packed
        candidates = {db_key: hashX for db_key, hashX
                      in self.db.utxo_db.iterator(prefix=prefix)}

        for hdb_key, candidate_value in candidates.items():
            tx_num_packed = hdb_key[-5:]
            hashX = candidate_value[:HASHX_LEN]
            asset_id = candidate_value[HASHX_LEN:]
            assert len(asset_id) == 4

            if len(candidates) > 1:
                tx_num, = unpack_le_uint64(tx_num_packed + bytes(3))
                fs_hash, _height = self.db.fs_tx_hash(tx_num)
                if fs_hash != tx_hash:
                    assert fs_hash is not None  # Should always be found
                    continue

            # Key: b'u' + address_hashX + tx_idx + tx_num
            # Value: the UTXO value as a 64-bit unsigned integer
            udb_key = PREFIX_HASHX_LOOKUP + hashX + asset_id + hdb_key[-9:]
            utxo_value_packed = self.db.utxo_db.get(udb_key)
            if utxo_value_packed:
                # Remove both entries for this UTXO
                self.utxo_deletes.append(hdb_key)
                self.utxo_deletes.append(udb_key)
                return hashX + tx_num_packed + utxo_value_packed + asset_id
           
        raise ChainError(f'UTXO {hash_to_hex_str(tx_hash)} / {tx_idx:,d} not found in "h" table')

    async def on_caught_up(self):
        was_first_sync = self.state.first_sync
        self.state.first_sync = False
        # Only flush if NOT caught_up yet (first sync) or has pending blocks
        # When caught_up, check_cache_size_loop handles all flushing
        if not self.caught_up and self.headers:
            await self.flush(True)
        if not self.caught_up:
            self.caught_up = True
            if was_first_sync:
                logger.info(f'{electrumx.version} synced to height {self.state.height:,d}')
            # Reopen for serving
            await self.db.open_for_serving()

    # --- External API

    async def fetch_and_process_blocks(self, caught_up_event, shutdown_event):
        '''Fetch, process and index blocks from the daemon.

        Sets caught_up_event when first caught up.  Flushes to disk
        and shuts down cleanly if cancelled.

        This is mainly because if, during initial sync ElectrumX is
        asked to shut down when a large number of blocks have been
        processed but not written to disk, it should write those to
        disk before exiting, as otherwise a significant amount of work
        could be lost.
        '''

        if self.env.write_bad_vouts_to_file and not os.path.isdir(self.bad_vouts_path):
            os.mkdir(self.bad_vouts_path)
        
        self.state = OnDiskBlock.state = (await self.db.open_for_sync()).copy()
        await OnDiskBlock.scan_files()
        
        try:
            show_summary = True
            while True:
                hex_hashes, daemon_height = await self.next_block_hashes()
                if show_summary:
                    show_summary = False
                    behind = daemon_height - self.state.height                    
                    if behind > 0:
                        logger.info(f'catching up to daemon height {daemon_height:,d} '
                                    f'({behind:,d} blocks behind)')
                    else:
                        logger.info(f'caught up to daemon height {daemon_height:,d}')

                if hex_hashes:
                    # Shielded so that cancellations from shutdown don't lose work
                    await self.advance_blocks(hex_hashes)
                else:
                    await self.on_caught_up()
                    caught_up_event.set()
                    await sleep(self.polling_delay)

                if self.reorg_count is not None:
                    await self.reorg_chain(self.reorg_count)
                    self.reorg_count = None
                    show_summary = True

        # Don't flush for arbitrary exceptions as they might be a cause or consequence of
        # corrupted data
        except CancelledError:
            await OnDiskBlock.stop_prefetching()
            await self.run_with_lock(self.flush_if_safe())
        except Exception:
            logging.exception('Critical Block Processor Error:')
            raise

    async def flush_if_safe(self):
        if self.ok:
            logger.info('flushing to DB for a clean shutdown...')
            await self.flush(True)
            logger.info('flushed cleanly')
        else:
            logger.warning('not flushing to DB as data in memory is incomplete')


    def force_chain_reorg(self, count):
        '''Force a reorg of the given number of blocks.  Returns True if a reorg is queued.
        During initial sync we don't store undo information so cannot fake a reorg until
        caught up.
        '''
        if self.caught_up:
            self.reorg_count = count
            return True
        return False

import asyncio
import secrets
from typing import Dict, Union
from pyrogram import Client, utils, raw
from pyrogram.errors import AuthBytesInvalid
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.session import Session, Auth
from Backend.logger import LOGGER
from Backend.helper.exceptions import FIleNotFound
from Backend.helper.pyro import get_file_ids
from Backend.pyrofork.bot import work_loads

class ByteStreamer:
    """
    Handles streaming Telegram files via Pyrogram safely.
    Tracks active streams per user & bot.
    """

    def __init__(self, client: Client):
        self.client: Client = client
        self.__cached_file_ids: Dict[int, FileId] = {}
        self.__active_streams: Dict[int, asyncio.Task] = {}  # user_id/message_id -> task
        self.clean_timer = 30 * 60
        asyncio.create_task(self.clean_cache())

    async def get_file_properties(self, chat_id: int, message_id: int) -> FileId:
        if message_id not in self.__cached_file_ids:
            file_id = await get_file_ids(self.client, int(chat_id), int(message_id))
            if not file_id:
                LOGGER.info('Message with ID %s not found!', message_id)
                raise FIleNotFound
            self.__cached_file_ids[message_id] = file_id
        return self.__cached_file_ids[message_id]

    async def yield_file(
        self,
        file_id: FileId,
        index: int,
        offset: int,
        first_part_cut: int,
        last_part_cut: int,
        part_count: int,
        chunk_size: int,
        request=None,  # FastAPI Request object
        user_key=None,  # Unique key to track user/bot session
    ):
        """
        Async generator yielding file chunks with auto-disconnect handling.
        Cancels previous stream for same user_key.
        """
        # Cancel previous stream if exists
        if user_key and user_key in self.__active_streams:
            prev_task = self.__active_streams[user_key]
            if not prev_task.done():
                LOGGER.debug(f"Cancelling previous stream for {user_key}")
                prev_task.cancel()
                try:
                    await prev_task
                except asyncio.CancelledError:
                    LOGGER.debug(f"Previous stream for {user_key} cancelled successfully")

        async def _stream_generator():
            client = self.client
            work_loads[index] += 1
            LOGGER.debug(f"Starting to yield file with client {index}.")

            # Check if already disconnected before even starting
            if request and await request.is_disconnected():
                LOGGER.debug(f"Client already disconnected before stream start for {user_key}")
                work_loads[index] -= 1
                return

            media_session = await self.generate_media_session(client, file_id)
            if not media_session:
                work_loads[index] -= 1
                return

            current_part = 1
            location = await self.get_location(file_id)
            try:
                r = await media_session.send(
                    raw.functions.upload.GetFile(location=location, offset=offset, limit=chunk_size)
                )
                if isinstance(r, raw.types.upload.File):
                    while True:
                        # Stop if client disconnected
                        if request and await request.is_disconnected():
                            LOGGER.debug(f"Client disconnected, stopping stream for {user_key}")
                            break

                        chunk = r.bytes
                        if not chunk:
                            break
                        elif part_count == 1:
                            yield chunk[first_part_cut:last_part_cut]
                        elif current_part == 1:
                            yield chunk[first_part_cut:]
                        elif current_part == part_count:
                            yield chunk[:last_part_cut]
                        else:
                            yield chunk

                        current_part += 1
                        offset += chunk_size

                        if current_part > part_count:
                            break

                        r = await media_session.send(
                            raw.functions.upload.GetFile(location=location, offset=offset, limit=chunk_size)
                        )
            except asyncio.CancelledError:
                LOGGER.debug(f"Stream cancelled for {user_key}")
                raise
            except (TimeoutError, AttributeError) as e:
                LOGGER.debug(f"Stream error for {user_key}: {e}")
            finally:
                LOGGER.debug(f"Finished yielding file with {current_part} parts for {user_key}.")
                work_loads[index] -= 1
                # Remove active stream tracking
                if user_key and user_key in self.__active_streams:
                    del self.__active_streams[user_key]

        task = asyncio.create_task(_stream_generator())
        if user_key:
            self.__active_streams[user_key] = task
        try:
            async for chunk in task:
                yield chunk
        except asyncio.CancelledError:
            LOGGER.debug(f"Stream task for {user_key} cancelled mid-yield")
            raise
        except Exception as e:
            LOGGER.error(f"Error during streaming for {user_key}: {e}")

    async def generate_media_session(self, client: Client, file_id: FileId) -> Session:
        media_session = client.media_sessions.get(file_id.dc_id, None)
        if media_session is None:
            if file_id.dc_id != await client.storage.dc_id():
                media_session = Session(
                    client,
                    file_id.dc_id,
                    await Auth(client, file_id.dc_id, await client.storage.test_mode()).create(),
                    await client.storage.test_mode(),
                    is_media=True,
                )
                await media_session.start()
                for _ in range(6):
                    exported_auth = await client.invoke(raw.functions.auth.ExportAuthorization(dc_id=file_id.dc_id))
                    try:
                        await media_session.send(
                            raw.functions.auth.ImportAuthorization(
                                id=exported_auth.id, bytes=exported_auth.bytes
                            )
                        )
                        break
                    except AuthBytesInvalid:
                        LOGGER.debug(f"Invalid auth bytes for DC {file_id.dc_id}, retrying...")
                    except OSError:
                        LOGGER.debug(f"Connection error, retrying...")
                        await asyncio.sleep(2)
                else:
                    await media_session.stop()
                    LOGGER.debug(f"Failed to establish media session for DC {file_id.dc_id}")
                    return None
            else:
                media_session = Session(
                    client,
                    file_id.dc_id,
                    await client.storage.auth_key(),
                    await client.storage.test_mode(),
                    is_media=True,
                )
                await media_session.start()
            client.media_sessions[file_id.dc_id] = media_session
            LOGGER.debug(f"Created media session for DC {file_id.dc_id}")
        else:
            LOGGER.debug(f"Using cached media session for DC {file_id.dc_id}")
        return media_session

    @staticmethod
    async def get_location(file_id: FileId) -> Union[raw.types.InputPhotoFileLocation, raw.types.InputDocumentFileLocation, raw.types.InputPeerPhotoFileLocation]:
        file_type = file_id.file_type
        if file_type == FileType.CHAT_PHOTO:
            if file_id.chat_id > 0:
                peer = raw.types.InputPeerUser(user_id=file_id.chat_id, access_hash=file_id.chat_access_hash)
            else:
                if file_id.chat_access_hash == 0:
                    peer = raw.types.InputPeerChat(chat_id=-file_id.chat_id)
                else:
                    peer = raw.types.InputPeerChannel(channel_id=utils.get_channel_id(file_id.chat_id),
                                                      access_hash=file_id.chat_access_hash)
            location = raw.types.InputPeerPhotoFileLocation(
                peer=peer,
                volume_id=file_id.volume_id,
                local_id=file_id.local_id,
                big=file_id.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG
            )
        elif file_type == FileType.PHOTO:
            location = raw.types.InputPhotoFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size
            )
        else:
            location = raw.types.InputDocumentFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size
            )
        return location

    async def clean_cache(self) -> None:
        while True:
            await asyncio.sleep(self.clean_timer)
            self.__cached_file_ids.clear()
            LOGGER.debug("Cleaned cached file IDs")
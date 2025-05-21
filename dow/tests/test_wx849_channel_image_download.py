import asyncio
import base64
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, AsyncMock, MagicMock
import sys

# Ensure project root is in path for imports when run with `python -m unittest`
project_root_for_imports = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if project_root_for_imports not in sys.path:
    sys.path.insert(0, project_root_for_imports)

# Attempt to import the target class and dependencies
# Adjust the import path based on the project structure if necessary
try:
    from dow.channel.wx849.wx849_channel import WX849Channel
    from dow.channel.wx849.wx849_message import WX849Message # For creating cmsg objects
    from dow.config import Config # For conf()
    # Mock conf() early to avoid issues with its initialization if it reads files
    mock_conf = MagicMock(spec=Config)
    mock_conf.get.side_effect = lambda key, default=None: {
        "wx849_api_host": "127.0.0.1",
        "wx849_api_port": 9011,
        "wx849_protocol_version": "849", # or "855" / "ipad" as needed for API path
        "log_level": "DEBUG" 
    }.get(key, default)

    # Patch conf() globally for the test module
    # This is tricky because conf is often imported as 'from config import conf'
    # For robust patching, it's better if WX849Channel takes conf as a dependency or uses a module-level get_conf()
    # Assuming direct import for now, this might need adjustment if tests fail due to config issues
    # config_patch = patch('dow.channel.wx849.wx849_channel.conf', mock_conf)
    # config_patch.start() # Start patch before class definition if WX849Channel uses conf at class level
    
    # Simpler approach: Patch where it's directly used if possible, or ensure conf() is callable
    # If WX849Channel calls config.conf(), then patch 'dow.channel.wx849.wx849_channel.config.conf'
    # If it calls conf(), then patch 'dow.channel.wx849.wx849_channel.conf'
    # Patching 'dow.channel.wx849.wx849_channel.conf' based on typical usage
    # global_conf_patch = patch('dow.channel.wx849.wx849_channel.conf', return_value=mock_conf) # Keep this commented
    # global_conf_patch.start()


except ImportError as e:
    print(f"Initial error importing necessary modules: {e}. PYTHONPATH: {os.environ.get('PYTHONPATH')}, sys.path: {sys.path}")
    # Fallback for basic structure if imports fail, allowing file creation
    class WX849Channel: pass 
    class WX849Message: pass
    class Config: pass # Define a fallback Config class
    mock_conf = MagicMock(spec=Config) # Initialize mock_conf even in fallback

# A very small, valid base64 encoded HEIC file (e.g., a 1x1 pixel HEIC)
# This is a placeholder. A real, small HEIC encoded to base64 would be needed.
# For now, we'll use a string that simulates HEIC by its magic bytes primarily.
# Real HEIC data would be much longer.
# Header: ....ftypheic
SAMPLE_HEIC_MAGIC_BYTES_PAYLOAD = b'\x00\x00\x00\x18ftypheic\x00\x00\x00\x00meta\x00\x00\x00\x00' 
# To make it a bit more realistic for pillow-heif, we might need a more complete minimal HEIC structure.
# For this test, we'll focus on magic bytes and mock pillow-heif's behavior if direct data is too complex.
SAMPLE_HEIC_B64 = base64.b64encode(SAMPLE_HEIC_MAGIC_BYTES_PAYLOAD + b'A'*200).decode('utf-8') # Added padding

# A very small, valid base64 encoded JPEG file (e.g., a 1x1 pixel JPEG)
SAMPLE_JPEG_B64 = "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/2wBDAQMDAwQDBAgEBAgQCwkLEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBD/wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAn/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFAEBAAAAAAAAAAAAAAAAAAAAAP/EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwEAAhEDEQA/AKpgA//Z"


@patch('dow.channel.wx849.wx849_channel.conf', mock_conf) # Patch conf where it's used
class TestWX849ChannelImageDownload(unittest.TestCase):

    def setUp(self):
        # Create a temporary directory for downloads
        self.test_dir = tempfile.mkdtemp()
        
        # Ensure the WX849Channel is initialized *after* conf is patched for this class
        self.channel = WX849Channel()
        
        # Mock the bot and wxid for the channel instance
        self.channel.bot = AsyncMock() 
        self.channel.wxid = "test_bot_wxid"
        
        # Override tmp_dir for tests by patching get_appdata_dir if it's used internally
        # or by directly setting it if the class allows.
        # For _download_image_by_chunks, it seems to form path using os.path.dirname three times
        # and then "tmp/images". We need to ensure this path is writable or mock it.
        # A robust way is to patch 'get_appdata_dir' if it's used to construct 'tmp_dir'
        # For now, we assume self.test_dir will be used by the patched methods or by direct configuration.
        # The method _download_image_by_chunks itself creates tmp_dir = os.path.dirname(image_path)
        # so we just need to ensure image_path is within self.test_dir.

        # Critical: Ensure conf() calls within WX849Channel methods use the mocked version.
        # This is handled by the class-level patch if conf is accessed as self.conf() or module.conf()
        # If conf is imported and called directly as conf(), the global_conf_patch (commented out) would be one way.

    def tearDown(self):
        # Remove the temporary directory
        shutil.rmtree(self.test_dir)
        # try:
        #     if 'global_conf_patch' in globals() and global_conf_patch.is_started:
        #          global_conf_patch.stop() 
        # except NameError:
        #     pass


    @patch('dow.channel.wx849.wx849_channel.aiohttp.ClientSession')
    @patch('dow.channel.wx849.wx849_channel.heif_available', True) 
    @patch('dow.channel.wx849.wx849_channel.pillow_heif.register_heif_opener', MagicMock())
    @patch('dow.channel.wx849.wx849_channel.Image.open') 
    def test_download_heic_image_and_convert(self, mock_image_open, mock_aiohttp_session, mock_class_conf):
        # --- Setup Mock API Response to return HEIC data ---
        mock_session_instance = AsyncMock()
        mock_post_response = AsyncMock()
        mock_post_response.status = 200
        api_chunk_response = {
            "Success": True,
            "Data": {"buffer": SAMPLE_HEIC_B64} 
        }
        mock_post_response.json = AsyncMock(return_value=api_chunk_response)
        mock_session_instance.post = AsyncMock(return_value=mock_post_response)
        mock_aiohttp_session.return_value.__aenter__.return_value = mock_session_instance

        # --- Setup Mock PIL.Image.open behavior ---
        mock_heic_image_instance = MagicMock()
        mock_heic_image_instance.format = "HEIC"
        mock_heic_image_instance.mode = "RGBA" 
        
        mock_jpeg_image_instance = MagicMock()
        mock_jpeg_image_instance.format = "JPEG"
        
        self.is_simulating_heic_conversion = True # Flag to guide side_effect

        def image_open_side_effect(path):
            if self.is_simulating_heic_conversion:
                # First call (on original path for detection) -> HEIC
                # Second call (during conversion, on original path) -> HEIC
                # Third call (validation, on new .jpg path) -> JPEG
                if mock_image_open.call_count <= 2 and (path.endswith(".tmp") or "heic" in path.lower()): # Detection or conversion open
                    return mock_heic_image_instance
                elif path.endswith(".jpg"): # Validation open
                    return mock_jpeg_image_instance
                else: # Fallback, should ideally not be reached in this specific test flow
                    return mock_heic_image_instance 
            else: # Simulating JPEG or other non-HEIC
                return mock_jpeg_image_instance


        mock_image_open.side_effect = image_open_side_effect
        
        # --- Create a mock cmsg object ---
        mock_msg_data = {
            "MsgId": "test_heic_msg_id_123",
            "FromUserName": "test_sender_wxid", # This will be cmsg.from_user_id
            "ToUserName": self.channel.wxid,
            "Content": "<msg><img aeskey=\"\" cdnmidimgurl=\"\" length=\"220\" md5=\"\" /></msg>" 
        }
        cmsg = WX849Message(mock_msg_data, is_group=False)
        cmsg.image_info = {'length': '220'} 
        # Ensure from_user_id is set correctly for DownloadImg API call if it uses it
        # cmsg.from_user_id = "test_sender_wxid" # Already set by WX849Message constructor

        # --- Run the download function ---
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        initial_download_path = os.path.join(self.test_dir, "initial_heic_download.tmp")
        download_result = loop.run_until_complete(self.channel._download_image_by_chunks(cmsg, initial_download_path))
        
        # --- Assertions ---
        self.assertTrue(download_result, "Download function should return True for success.")
        
        self.assertTrue(mock_image_open.call_count >= 2, f"Image.open call count was {mock_image_open.call_count}") 

        mock_heic_image_instance.convert.assert_called_with('RGB')
        mock_heic_image_instance.save.assert_called_with(unittest.mock.ANY, "JPEG")
        
        saved_path = mock_heic_image_instance.save.call_args[0][0]
        self.assertTrue(saved_path.endswith(".jpg"))
        
        self.assertEqual(cmsg.image_path, saved_path)
        self.assertEqual(cmsg.content, saved_path) # Assuming cmsg.content is also updated

    @patch('dow.channel.wx849.wx849_channel.aiohttp.ClientSession')
    @patch('dow.channel.wx849.wx849_channel.heif_available', True)
    @patch('dow.channel.wx849.wx849_channel.pillow_heif.register_heif_opener', MagicMock())
    @patch('dow.channel.wx849.wx849_channel.Image.open')
    def test_download_jpeg_image_no_conversion(self, mock_image_open, mock_aiohttp_session, mock_class_conf):
        # --- Setup Mock API Response to return JPEG data ---
        mock_session_instance = AsyncMock()
        mock_post_response = AsyncMock()
        mock_post_response.status = 200
        api_chunk_response = {
            "Success": True,
            "Data": {"buffer": SAMPLE_JPEG_B64} 
        }
        mock_post_response.json = AsyncMock(return_value=api_chunk_response)
        mock_session_instance.post = AsyncMock(return_value=mock_post_response)
        mock_aiohttp_session.return_value.__aenter__.return_value = mock_session_instance

        # --- Setup Mock PIL.Image.open behavior for JPEG ---
        mock_jpeg_image_instance = MagicMock()
        mock_jpeg_image_instance.format = "JPEG" 
        
        self.is_simulating_heic_conversion = False # Flag for side_effect
        mock_image_open.side_effect = lambda path: mock_jpeg_image_instance # Always return JPEG mock

        # --- Create a mock cmsg object ---
        initial_filename = "test_jpeg_image.jpg" # Assume it's already a JPG
        initial_image_path = os.path.join(self.test_dir, initial_filename)

        mock_msg_data = {
            "MsgId": "test_jpeg_msg_id_456",
            "FromUserName": "test_sender_wxid",
            "ToUserName": self.channel.wxid,
            "Content": f"<msg><img aeskey=\"\" cdnmidimgurl=\"\" length=\"150\" md5=\"\" /></msg>"
        }
        cmsg = WX849Message(mock_msg_data, is_group=False)
        cmsg.image_info = {'length': '150'}
        # cmsg.from_user_id = "test_sender_wxid" # Set by constructor

        # --- Run the download function ---
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        download_result = loop.run_until_complete(self.channel._download_image_by_chunks(cmsg, initial_image_path))

        # --- Assertions ---
        self.assertTrue(download_result)
        
        # Image.open called for detection and validation
        self.assertTrue(mock_image_open.call_count >= 1) 
        
        mock_jpeg_image_instance.convert.assert_not_called()
        mock_jpeg_image_instance.save.assert_not_called()
        
        self.assertEqual(cmsg.image_path, initial_image_path)
        self.assertEqual(cmsg.content, initial_image_path)


if __name__ == '__main__':
    # This allows running the test file directly
    # You might need to adjust PYTHONPATH for imports to work:
    # Example: sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))) # Original line
    # before the dow imports.
    
    # The sys.path modification is now at the top of the file.
    # The following re-import attempt might be redundant if the top-level one works,
    # but it doesn't hurt to leave it for direct script execution context.
    # Attempt to make imports work when run directly (if not already via top-level sys.path mod)
    # import sys # sys already imported
    project_root_main = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    if project_root_main not in sys.path:
        sys.path.insert(0, project_root_main)
        print(f"Running as main, added {project_root_main} to sys.path for re-imports.")

    # Need to re-import after path adjustment if imports failed initially
    try:
        from dow.channel.wx849.wx849_channel import WX849Channel
        from dow.channel.wx849.wx849_message import WX849Message
        from dow.config import Config # Should be found if /app is project_root
    except ImportError as main_import_error:
        print(f"Failed to re-import dow modules in __main__. Error: {main_import_error}. sys.path: {sys.path}")

    unittest.main()

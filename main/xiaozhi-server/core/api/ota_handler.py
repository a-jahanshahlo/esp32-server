import json
import time
import base64
import hashlib
import hmac
from aiohttp import web

from core.auth import AuthManager
from core.utils.util import get_local_ip
from core.api.base_handler import BaseHandler

TAG = __name__


class OTAHandler(BaseHandler):
    def __init__(self, config: dict):
        super().__init__(config)
        auth_config = config["server"].get("auth", {})
        self.auth_enable = auth_config.get("enabled", False)
        # Device whitelist
        self.allowed_devices = set(auth_config.get("allowed_devices", []))
        secret_key = config["server"]["auth_key"]
        expire_seconds = auth_config.get("expire_seconds")
        self.auth = AuthManager(secret_key=secret_key, expire_seconds=expire_seconds)

    def generate_password_signature(self, content: str, secret_key: str) -> str:
        """Generate MQTT cryptographic signature

        Args:
            content: signature content (clientId + '|' + username)
            secret_key: key

        Returns:
            str: Base64 encoded HMAC-SHA256 signature
        """
        try:
            hmac_obj = hmac.new(
                secret_key.encode("utf-8"), content.encode("utf-8"), hashlib.sha256
            )
            signature = hmac_obj.digest()
            return base64.b64encode(signature).decode("utf-8")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Failed to generate mqtt password signature: {e}")
            return ""

    def _get_websocket_url(self, local_ip: str, port: int) -> str:
        """Get websocket address

        Args:
            local_ip: local IP address
            port: port number

        Returns:
            str: websocket address
        """
        server_config = self.config["server"]
        websocket_config = server_config.get("websocket", "")

        if "your" not in websocket_config:
            return websocket_config
        else:
            return f"ws://{local_ip}:{port}/xiaozhi/v1/"

    async def handle_post(self, request):
        """Handling OTA POST requests"""
        try:
            data = await request.text()
            self.logger.bind(tag=TAG).debug(f"Ota request method: {request.method}")
            self.logger.bind(tag=TAG).debug(f"Ota request header: {request.headers}")
            self.logger.bind(tag=TAG).debug(f"Ota request data: {data}")

            device_id = request.headers.get("device-id", "")
            if device_id:
                self.logger.bind(tag=TAG).info(f"Ota requests device id: {device_id}")
            else:
                raise Exception("Ota request device id is empty")

            client_id = request.headers.get("client-id", "")
            if client_id:
                self.logger.bind(tag=TAG).info(f"Ota requests client id: {client_id}")
            else:
                raise Exception("Ota request client id is empty")

            data_json = json.loads(data)

            server_config = self.config["server"]
            port = int(server_config.get("port", 8000))
            local_ip = get_local_ip()

            return_json = {
                "server_time": {
                    "timestamp": int(round(time.time() * 1000)),
                    "timezone_offset": server_config.get("timezone_offset", 8) * 60,
                },
                "firmware": {
                    "version": data_json["application"].get("version", "1.0.0"),
                    "url": "",
                },
            }

            mqtt_gateway_endpoint = server_config.get("mqtt_gateway")

            if mqtt_gateway_endpoint:  # If a non-empty string is configured
                # Try to get the device model from the request data
                device_model = "default"
                try:
                    if "device" in data_json and isinstance(data_json["device"], dict):
                        device_model = data_json["device"].get("model", "default")
                    elif "model" in data_json:
                        device_model = data_json["model"]
                    group_id = f"GID_{device_model}".replace(":", "_").replace(" ", "_")
                except Exception as e:
                    self.logger.bind(tag=TAG).error(f"Failed to obtain device model: {e}")
                    group_id = "GID_default"

                mac_address_safe = device_id.replace(":", "_")
                mqtt_client_id = f"{group_id}@@@{mac_address_safe}@@@{mac_address_safe}"

                # Build user data
                user_data = {"ip": "unknown"}
                try:
                    user_data_json = json.dumps(user_data)
                    username = base64.b64encode(user_data_json.encode("utf-8")).decode(
                        "utf-8"
                    )
                except Exception as e:
                    self.logger.bind(tag=TAG).error(f"Failed to generate username: {e}")
                    username = ""

                # Generate password
                password = ""
                signature_key = server_config.get("mqtt_signature_key", "")
                if signature_key:
                    password = self.generate_password_signature(
                        mqtt_client_id + "|" + username, signature_key
                    )
                    if not password:
                        password = ""  # If the signature fails, leave it blank and the device will decide whether to allow no password.
                else:
                    self.logger.bind(tag=TAG).warning("缺少MQTT签名密钥，密码留空")

                #Build MQTT configuration (using mqtt_gateway string directly)
                return_json["mqtt_gateway"] = {
                    "endpoint": mqtt_gateway_endpoint,
                    "client_id": mqtt_client_id,
                    "username": username,
                    "password": password,
                    "publish_topic": "device-server",
                    "subscribe_topic": f"devices/p2p/{mac_address_safe}",
                }
                self.logger.bind(tag=TAG).info(f"为设备 {device_id} 下发MQTT网关配置")

            else:  # mqtt_gateway is not configured, and WebSocket is delivered
                # If authentication is turned on, perform authentication verification
                token = ""
                if self.auth_enable:
                    if self.allowed_devices:
                        if device_id not in self.allowed_devices:
                            token = self.auth.generate_token(client_id, device_id)
                    else:
                        token = self.auth.generate_token(client_id, device_id)
                return_json["websocket"] = {
                    "url": self._get_websocket_url(local_ip, port),
                    "token": token,
                }
                self.logger.bind(tag=TAG).info(
                    f"The MQTT gateway is not configured, and the WebSocket configuration is delivered to the device {device_id}."
                )
                self.logger.bind(tag=TAG).info(f"{return_json}")

            response = web.Response(
                text=json.dumps(return_json, separators=(",", ":")),
                content_type="application/json",
            )
        except Exception as e:
            return_json = {"success": False, "message": "request error."}
            response = web.Response(
                text=json.dumps(return_json, separators=(",", ":")),
                content_type="application/json",
            )
        finally:
            self._add_cors_headers(response)
            return response

    async def handle_get(self, request):
        """Handling OTA GET requests"""
        try:
            server_config = self.config["server"]
            local_ip = get_local_ip()
            port = int(server_config.get("port", 8000))
            websocket_url = self._get_websocket_url(local_ip, port)
            message = f"The Ota interface is running normally, and the websocket address sent to the device is: {websocket url}"
            response = web.Response(text=message, content_type="text/plain")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"OTA GET request exception: {e}")
            response = web.Response(text="Ota interface exception", content_type="text/plain")
        finally:
            self._add_cors_headers(response)
            return response

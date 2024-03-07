"""XMPP Connector to talk to bosch."""

import logging
import json
import re
from slixmpp import ClientXMPP, Iq
from slixmpp.exceptions import IqError, IqTimeout
from slixmpp.xmlstream.handler import Callback
from slixmpp.xmlstream.matcher import StanzaPath
import asyncio
from bosch_thermostat_client.exceptions import (
    DeviceException,
    MsgException,
    EncryptionException,
)
from bosch_thermostat_client.const import (
    GET,
    PUT,
    REQUEST_TIMEOUT,
    BODY_400,
    WRONG_ENCRYPTION,
    ACCESS_KEY,
)

_LOGGER = logging.getLogger(__name__)


class BoschClientXMPP(ClientXMPP):

    def __init__(self, jid, password, ca_certs, **kwargs):
        ClientXMPP.__init__(self, jid=jid, password=password, **kwargs)
        self.ca_certs = ca_certs


class XMPPBaseConnector:
    ca_certs = None

    def __init__(self, host, encryption, **kwargs):
        """
        :param host: aka serial number
        :param password:
        """
        self.serial_number = host
        self._encryption = encryption
        self._lock = asyncio.Lock()

        identifier = self.serial_number + "@" + self.xmpp_host
        self._from = self._rrc_contact_prefix + identifier

        self._to = self._rrc_gateway_prefix + identifier
        self._password = self._accesskey_prefix + kwargs.get(ACCESS_KEY)
        self.client = BoschClientXMPP(jid=self._from, password=self._password, ca_certs=self.ca_certs)
        self.client.register_plugin("xep_0030")  # Service Discovery
        self.client.register_plugin("xep_0199")  # XMPP Ping
        self.client.add_event_handler("session_start", self.session_start)
        self.client.add_event_handler("session_end", self.session_end)

        self.client.add_event_handler("message", self.main_listener)
        self.client.register_handler(
            Callback(
                "Query Request",
                StanzaPath("iq@type=get"),
                self.handle_query_request,
            )
        )
        self.client.add_event_handler(
            "ssl_invalid_chain", self.discard_ssl_invalid_chain
        )
        self.connected_event = asyncio.Event()
        self.disconnect_event = asyncio.Event()
        self.session_started = False
        self.received_message = None

        self.listeners = set()

    def handle_query_request(self, iq: Iq):
        query = iq.get_query()
        reply = iq.reply()
        if query == "jabber:iq:version":
            reply["xmlns"] = "jabber:iq:version"
            reply["version"] = "-1364755535"
            reply.send()
        if query == "com.bosch.tt.buderus.controlng":
            reply["xmlns"] = "com.bosch.tt.buderus.controlng"
            reply["name"] = "3.6.0"
            reply["version"] = "3.6.0"
            reply["os"] = ""
            reply.send()

    async def close(self, force):
        self.client.disconnect()
        await asyncio.wait_for(self.disconnect_event.wait(), 10)

    async def session_start(self, event):
        self.client.send_presence()
        self.client.get_roster()
        self.session_started = True
        self.connected_event.set()

    async def session_end(self, event):
        self.disconnect_event.set()

    @property
    def encryption_key(self):
        return self._encryption.key

    def _build_message(self, method, path, data=None):
        pass

    async def get(self, path):
        _LOGGER.debug("Sending GET request to %s by %s", path, id(self))
        data = await self._request(method=GET, path=path)
        _LOGGER.debug("Response to GET request %s: %s", path, json.dumps(data))
        if not data:
            raise DeviceException(f"Error requesting data from {path}")
        return data

    async def put(self, path, value):
        _LOGGER.debug("Sending PUT request to %s with value %s", path, value)
        data = await self._request(
            method=PUT,
            encrypted_msg=self._encryption.encrypt(json.dumps({"value": value})),
            path=path,
            timeout=10,
        )
        if data:
            return True

    async def _request(self, method, path, encrypted_msg=None, timeout=REQUEST_TIMEOUT):
        data = None
        try:
            if not self.session_started:
                self.client.connect(
                    use_ssl=self.use_ssl, force_starttls=self.force_starttls, disable_starttls=self.disable_starttls
                )
                await asyncio.wait_for(self.connected_event.wait(), timeout=10)
        except asyncio.TimeoutError:
            _LOGGER.error(
                "Can't connect to XMPP server!. Check your network connection or credentials!"
            )
            return None
        await self._lock.acquire()
        msg_to_send = self._build_message(method=method, path=path, data=encrypted_msg)
        try:
            future = asyncio.Future()

            def listener(recv_body, http_response):
                if future.done() or future.cancelled():
                    _LOGGER.debug(
                        "Future is already done. If it happens too often that it might be a bug. Report it."
                    )
                    return
                if method == PUT and http_response == "HTTP/1.0 204 No Content":
                    future.set_result(True)
                if recv_body == BODY_400:
                    future.set_exception(MsgException("400 HTTP Error"))
                elif recv_body is None and http_response == WRONG_ENCRYPTION:
                    future.set_exception(
                        EncryptionException("Can't decrypt for %s" % path)
                    )
                elif method == GET and recv_body.get("id") in path:
                    try:
                        future.set_result(recv_body)
                    except asyncio.InvalidStateError as err:
                        _LOGGER.error(
                            "If you see this log please attach it to your issue/send it on Discord. Err message: %s. Recv body: %s, Path: %s",
                            err,
                            json.dumps(recv_body),
                            path,
                        )
                        pass

            try:
                self.listeners.add(listener)
                self.client.send_message(mto=self._to, mbody=msg_to_send, mtype="chat")
                data = await asyncio.wait_for(future, timeout)
            except IqError as e:
                _LOGGER.error("Error sending message: %s", e)
            except IqTimeout:
                _LOGGER.error("IqTimeout sending message")
            except (asyncio.TimeoutError, MsgException):
                _LOGGER.info("Msg exception for %s", path)
            except EncryptionException as err:
                _LOGGER.warn(err)
                raise EncryptionException(err)
            except asyncio.InvalidStateError as err:
                _LOGGER.error("Unknown error occured. Please check logs. %s", err)
            finally:
                self.listeners.remove(listener)
        finally:
            self._lock.release()
            return data

    def main_listener(self, msg):
        if msg["type"] not in ("normal", "chat"):
            return
        body = msg["body"]
        if not body:
            return

        def notify_error(body, response):
            if len(self.listeners) == 1:
                for listener in self.listeners:
                    listener(body, response)
                    break

        try:
            body_arr = body.split("\n")
        except AttributeError:
            return
        http_response = body_arr[0]
        if re.match(r"HTTP/1.[0-1] 20*", http_response):
            try:
                decrypted_body = self._encryption.json_decrypt(body_arr[-1:][0])
            except EncryptionException:
                notify_error(body=None, response=WRONG_ENCRYPTION)
            else:
                for listener in self.listeners:
                    listener(decrypted_body, http_response)
            return
        if re.match(r"HTTP/1.[0-1] 40*", http_response):
            _LOGGER.info(f"400 HTTP Error - {body_arr}")
            notify_error(body=BODY_400, response=http_response)
    
    @staticmethod
    def discard_ssl_invalid_chain(event):
        """Do nothing if ssl certificate is invalid."""
        _LOGGER.info("Ignoring invalid SSL certificate as requested")

"""XMPP Connector to talk to bosch."""

import aioxmpp
from bosch_thermostat_client.const import (
    PUT,
    GET,
    USER_AGENT,
    CONTENT_TYPE,
    APP_JSON,
    ACCESS_KEY,
)
from bosch_thermostat_client.const.easycontrol import EASYCONTROL
from .xmpp import XMPPBaseConnector

USERAGENT = "rrc2"


class EasycontrolConnector(XMPPBaseConnector):
    xmpp_host = "xmpp.rrcng.ticx.boschtt.net"
    _accesskey_prefix = "C42i9NNp_"
    _rrc_contact_prefix = "rrc2contact_"
    _rrc_gateway_prefix = "rrc2gateway_"
    no_verify = True
    device_type = EASYCONTROL

    def __init__(self, host, encryption, **kwargs):
        self._seqno = 0
        super().__init__(
            host=host,
            encryption=encryption,
            access_key=kwargs.get(ACCESS_KEY),
        )

    def _build_message(self, method, path, data=None):
        if not path:
            return
        if method == GET:
            body = "\n".join(
                [
                    f"GET {path} HTTP/1.1",
                    f"{USER_AGENT}: {USERAGENT}",
                    f"Seq-No: {self._seqno}",
                    "\n",
                ]
            )
        elif method == PUT and data:
            body = "\r".join(
                [
                    f"PUT {path} HTTP/1.1",
                    f"{USER_AGENT}: {USERAGENT}",
                    f"{CONTENT_TYPE}: {APP_JSON}",
                    f"Content-Length: {len(data)}",
                    "",
                    data.decode("utf-8"),
                    "\r",
                ]
            )
        else:
            return
        self._seqno += 1
        return body

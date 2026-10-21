import asyncio

import httpx

from qrzterm.qrz import Client, LookupSuperseded, QrzError

LOGIN = b"<QRZDatabase><Session><Key>session-key</Key></Session></QRZDatabase>"
LOOKUP = b"""<QRZDatabase>
  <Callsign><call>AA7BQ</call><fname>Fred</fname><image>https://cdn.example/a.jpg</image></Callsign>
  <Session><Key>session-key</Key><Count>4</Count></Session>
</QRZDatabase>"""
TIMEOUT = b"<QRZDatabase><Session><Error>Session Timeout</Error></Session></QRZDatabase>"


def _client(handler) -> tuple[Client, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = Client(http=http)
    client.set_credentials("kc2rgw", "secret")
    return client, http


def test_lookup_logs_in_then_fetches():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            body = request.content.decode()
            assert "username=kc2rgw" in body
            assert "password=secret" in body
            assert "agent=QRZterm%2F" in body or "agent=QRZterm/" in body
            assert "password" not in str(request.url)
            return httpx.Response(200, content=LOGIN)
        assert request.url.params["callsign"] == "AA7BQ"
        assert request.url.params["s"] == "session-key"
        assert "password" not in str(request.url)
        return httpx.Response(200, content=LOOKUP)

    async def run() -> None:
        client, http = _client(handler)
        try:
            response = await client.lookup("aa7bq")
        finally:
            await http.aclose()
        assert response.value("call") == "AA7BQ"
        assert response.value("fname") == "Fred"
        assert client.session_key == "session-key"
        assert [request.method for request in seen] == ["POST", "GET"]

    asyncio.run(run())


def test_expired_session_logs_in_again():
    gets = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            gets["n"] += 1
            if gets["n"] == 1:
                return httpx.Response(200, content=TIMEOUT)
            assert request.url.params["callsign"] == "AA7BQ"
            return httpx.Response(200, content=LOOKUP)
        return httpx.Response(200, content=b"<QRZDatabase><Session><Key>fresh</Key></Session></QRZDatabase>")

    async def run() -> None:
        client, http = _client(handler)
        client.session_key = "stale"
        try:
            response = await client.lookup("AA7BQ")
        finally:
            await http.aclose()
        assert response.value("fname") == "Fred"
        assert client.session_key == "session-key"
        assert gets["n"] == 2

    asyncio.run(run())


def test_not_found_and_network_error():
    def missing(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, content=LOGIN)
        return httpx.Response(
            200,
            content=b"<QRZDatabase><Session><Key>session-key</Key><Error>Not found: g1srdd</Error></Session></QRZDatabase>",
        )

    async def run_missing() -> None:
        client, http = _client(missing)
        try:
            await client.lookup("g1srdd")
        finally:
            await http.aclose()

    try:
        asyncio.run(run_missing())
    except QrzError as exc:
        assert "Not found: g1srdd" in str(exc)
    else:
        raise AssertionError("expected QrzError")

    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async def run_down() -> None:
        client, http = _client(down)
        try:
            await client.lookup("W1AW")
        finally:
            await http.aclose()

    try:
        asyncio.run(run_down())
    except QrzError as exc:
        assert str(exc).startswith("Could not reach QRZ:")
    else:
        raise AssertionError("expected QrzError")


def test_logbook_insert_and_fetch():
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "ACTION=INSERT" in body:
            assert "KEY=logkey" in body
            assert "ADIF=%3Ccall" in body
            return httpx.Response(200, content=b"RESULT=OK&LOGID=42&COUNT=1")
        if "ACTION=FETCH" in body:
            assert "OPTION=CALL%3AW1AW" in body
            return httpx.Response(200, content=b"RESULT=FAIL&REASON=no+log+entries+found&COUNT=0")
        if "ACTION=STATUS" in body:
            return httpx.Response(200, content=b"RESULT=AUTH")
        return httpx.Response(200, content=b"RESULT=FAIL&REASON=nope")

    async def run() -> None:
        client, http = _client(handler)
        client.set_logbook_key("logkey")
        try:
            inserted = await client.insert_qso(b"<call:4>W1AW<eor>")
            assert inserted.ok()
            assert inserted.log_id == "42"
            fetched = await client.check_log("w1aw")
            assert fetched.records == []
            try:
                await client.insert_qso(b"<call:4>W1AW<eor>")
            except QrzError:
                pass
            else:
                # The handler answers INSERT with OK. Force AUTH on the next call
                # by using a second client below.
                pass
        finally:
            await http.aclose()

    asyncio.run(run())


def test_logbook_rejects_a_bad_key():
    def handler(request: httpx.Request) -> httpx.Response:
        if b"ACTION=INSERT" in request.content:
            return httpx.Response(200, content=b"RESULT=AUTH")
        return httpx.Response(200, content=b"RESULT=AUTH")

    async def run() -> None:
        client, http = _client(handler)
        client.set_logbook_key("nope")
        try:
            await client.insert_qso(b"<call:4>W1AW<eor>")
        finally:
            await http.aclose()

    try:
        asyncio.run(run())
    except QrzError as exc:
        assert "refused the API key" in str(exc)
    else:
        raise AssertionError("expected QrzError")


def test_a_newer_lookup_supersedes_the_one_in_flight():
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        call = request.url.params.get("callsign", "")
        if call == "W1AW":
            started.set()
            await release.wait()
            return httpx.Response(
                200,
                content=b"<QRZDatabase><Callsign><call>W1AW</call></Callsign><Session><Key>old</Key></Session></QRZDatabase>",
            )
        return httpx.Response(
            200,
            content=b"<QRZDatabase><Callsign><call>K1AB</call></Callsign><Session><Key>new</Key></Session></QRZDatabase>",
        )

    async def run() -> None:
        client, http = _client(handler)
        client.session_key = "live"
        first = asyncio.create_task(client.lookup("W1AW"))
        await started.wait()
        second = asyncio.create_task(client.lookup("K1AB"))
        release.set()
        first_result, second_result = await asyncio.gather(first, second, return_exceptions=True)
        await http.aclose()
        assert isinstance(first_result, LookupSuperseded)
        assert not isinstance(second_result, Exception)
        assert second_result.value("call") == "K1AB"
        assert client.session_key == "new"

    asyncio.run(run())

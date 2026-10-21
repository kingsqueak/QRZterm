"""Parser and ADIF tests. The cases follow 590Ctrl's tests/tst_qrz.cpp."""

from datetime import datetime, timezone

from qrzterm.qrz import (
    MODE_NAMES,
    Field,
    LogInputError,
    LookupResponse,
    Qso,
    adif_band,
    adif_mode_named,
    adif_record,
    apply_lookup,
    default_report,
    display_fields,
    field_display_value,
    field_label,
    field_rank,
    full_name,
    grid_center,
    grouped_fields,
    local_time,
    path_from,
    qth_line,
    split_groups,
    form_encode,
    format_mhz,
    latest_qso,
    parse_adif,
    parse_fetch_response,
    parse_log_response,
    parse_lookup_response,
    parse_mhz,
    prepare_qso,
    worked_summary,
)

LOOKUP_XML = b"""<?xml version="1.0" encoding="utf-8" ?>
<QRZDatabase version="1.34" xmlns="http://xmldata.qrz.com">
  <Callsign>
    <call>AA7BQ</call>
    <fname>FRED L</fname>
    <name>LLOYD</name>
    <addr2>PHOENIX</addr2>
    <state>AZ</state>
    <county>Maricopa</county>
    <land>United States</land>
    <dxcc>291</dxcc>
    <grid>DM32af</grid>
    <cqzone>3</cqzone>
    <ituzone>6</ituzone>
    <lotw>1</lotw>
    <newfield>something</newfield>
  </Callsign>
  <Session>
    <Key>2331uf894c4bd29f3923f3bacf02c532d7bd9</Key>
    <Count>123</Count>
    <SubExp>Wed Jan 1 12:34:03 2025</SubExp>
    <GMTime>Sun Aug 16 03:51:47 2024</GMTime>
  </Session>
</QRZDatabase>"""


def test_parses_lookup():
    response = parse_lookup_response(LOOKUP_XML)
    assert response is not None
    assert response.key == "2331uf894c4bd29f3923f3bacf02c532d7bd9"
    assert response.lookup_count == "123"
    assert response.subscription_expiry == "Wed Jan 1 12:34:03 2025"
    assert response.value("call") == "AA7BQ"
    assert response.value("grid") == "DM32af"
    assert response.value("newfield") == "something"
    assert len(response.callsign) == 13
    assert response.error == ""
    assert field_label("fname") == "First name"
    assert field_label("newfield") == "newfield"
    assert field_rank("call") < field_rank("grid") < field_rank("newfield")
    assert field_display_value("lotw", "1") == "Yes"
    assert field_display_value("grid", "1") == "1"
    names = [item.name for item in display_fields(response)]
    assert names[0] == "call"
    assert names.index("grid") < names.index("newfield")


def test_parses_errors():
    not_found = parse_lookup_response(
        b"<QRZDatabase version=\"1.34\">\n"
        b"  <Session><Error>Not found: g1srdd</Error><Key>abc</Key></Session></QRZDatabase>"
    )
    assert not_found is not None
    assert not_found.callsign == []
    assert not_found.error == "Not found: g1srdd"
    assert not_found.key == "abc"

    expired = parse_lookup_response(b"<QRZDatabase><Session><Error>Session Timeout</Error></Session></QRZDatabase>")
    assert expired is not None
    assert expired.key == ""

    assert parse_lookup_response(b"<html>nope</html>") is None
    assert parse_lookup_response(b"") is None


def test_parses_logbook_answers():
    ok = parse_log_response(b"RESULT=OK&LOGIDS=130877825&COUNT=1\n")
    assert ok.ok()
    assert ok.log_id == "130877825"
    fail = parse_log_response(b"RESULT=FAIL&REASON=Unable+to+add%3A+duplicate&COUNT=0")
    assert not fail.ok()
    assert fail.reason == "Unable to add: duplicate"
    assert parse_log_response(b"RESULT=AUTH").result == "AUTH"


def test_parses_adif():
    records = parse_adif(
        "header text <adif_ver:5>3.1.6 <eoh>\n"
        "<CALL:4>W1AW <QSO_DATE:8:D>20250101<time_on:4>1200<mode:3>SSB <eor>\n"
        "<call:4>W1AW<qso_date:8>20261018<time_on:6>221425<comment:9>a <b> & c<eor>"
    )
    assert len(records) == 2
    assert records[0]["call"] == "W1AW"
    assert records[0]["qso_date"] == "20250101"
    assert records[1]["comment"] == "a <b> & c"
    latest = latest_qso(records)
    assert latest is not None
    assert latest["qso_date"] == "20261018"
    assert latest_qso([]) is None


def test_parses_fetch_answers():
    fetch = parse_fetch_response(
        b"RESULT=OK&COUNT=2&ADIF=&lt;call:4&gt;W1AW&lt;qso_date:8&gt;20250101&lt;time_on:4&gt;1200"
        b"&lt;band:3&gt;20m&lt;mode:3&gt;SSB&lt;eor&gt;\n&lt;call:4&gt;W1AW&lt;qso_date:8&gt;20240101"
        b"&lt;comment:5&gt;x &amp; y&lt;eor&gt;"
    )
    assert fetch.result == "OK"
    assert fetch.count == 2
    assert len(fetch.records) == 2
    assert fetch.records[1]["comment"] == "x & y"
    latest = latest_qso(fetch.records)
    assert latest is not None
    assert latest["band"] == "20m"

    encoded = parse_fetch_response(b"RESULT=OK&ADIF=%3Ccall%3A4%3EK1AB%3Ceor%3E&COUNT=1")
    assert len(encoded.records) == 1
    assert encoded.count == 1

    none = parse_fetch_response(b"RESULT=FAIL&REASON=no+log+entries+found&COUNT=0")
    assert none.result == "FAIL"
    assert none.records == []
    worked, detail = worked_summary(none)
    assert worked is False
    assert detail == "Not in your QRZ log"

    worked, detail = worked_summary(fetch)
    assert worked is True
    assert detail == "Last worked 2025-01-01 12:00 UTC · 20m SSB · 2 QSOs in your log"


def test_encodes_forms():
    assert form_encode([("ADIF", "<call:4>XX1X<eor>"), ("P", "a+b&c d")]) == (
        b"ADIF=%3Ccall%3A4%3EXX1X%3Ceor%3E&P=a%2Bb%26c%20d"
    )


def test_bands_and_modes():
    assert adif_band(14_074_000) == "20m"
    assert adif_band(3_856_000) == "80m"
    assert adif_band(5_357_000) == "60m"
    assert adif_band(50_313_000) == "6m"
    assert adif_band(9_500_000) == ""
    assert adif_band(146_520_000) == "2m"
    assert adif_band(432_000_000) == "70cm"
    assert "DATA" not in MODE_NAMES
    for mode in ("PSK", "MFSK", "OLIVIA", "THOR", "HELL"):
        assert mode in MODE_NAMES
        named = adif_mode_named(mode)
        assert named.mode == mode
        assert named.submode == ""
    assert adif_mode_named("ssb", "LSB").submode == "LSB"
    assert adif_mode_named("USB").mode == "SSB"
    assert adif_mode_named("USB").submode == "USB"
    assert adif_mode_named("FT4").mode == "MFSK"
    assert adif_mode_named("FT4").submode == "FT4"
    assert adif_mode_named("PSK31").mode == "PSK"
    assert adif_mode_named("cw").mode == "CW"
    assert default_report("CW") == "599"
    assert default_report("SSB") == "59"
    assert default_report("FT8") == ""


def test_parses_frequency_box():
    assert parse_mhz("14.195") == 14_195_000
    assert parse_mhz("14074") == 14_074_000
    assert parse_mhz("14") == 14_000_000
    assert parse_mhz("146.52") == 146_520_000
    assert parse_mhz("7.074 MHz") == 7_074_000
    assert parse_mhz("") == 0
    assert format_mhz(14_195_000) == "14.195000"


def test_builds_adif_record():
    qso = Qso(
        call="aa7bq",
        station_call="KC2RGW",
        time_on=datetime(2026, 10, 18, 23, 4, 5, tzinfo=timezone.utc),
        tx_hz=14_195_000,
        mode="SSB",
        submode="USB",
        rst_sent="59",
        rst_rcvd="57",
        tx_power=100,
    )
    assert adif_record(qso) == (
        b"<call:5>AA7BQ<qso_date:8>20261018<time_on:6>230405<band:3>20m<freq:9>14.195000"
        b"<mode:3>SSB<submode:3>USB<rst_sent:2>59<rst_rcvd:2>57<station_callsign:6>KC2RGW"
        b"<tx_pwr:3>100<eor>"
    )

    qso.rx_hz = 14_200_000
    lookup = LookupResponse(
        callsign=[
            Field("call", "AA7BQ"),
            Field("fname", "Fred"),
            Field("name", "Lloyd"),
            Field("state", "AZ"),
            Field("county", "Maricopa"),
            Field("grid", "DM32af"),
            Field("land", "United States"),
            Field("dxcc", "291"),
        ]
    )
    apply_lookup(qso, lookup)
    record = adif_record(qso)
    assert b"<freq_rx:9>14.200000" in record
    assert b"<name:10>Fred Lloyd" in record
    assert b"<cnty:11>AZ,Maricopa" in record
    assert b"<gridsquare:6>DM32af" in record
    assert b"<dxcc:3>291" in record
    assert record.endswith(b"<eor>")

    other = Qso(call="W1AW", station_call="KC2RGW", time_on=qso.time_on)
    apply_lookup(other, lookup)
    assert other.name == ""

    xref = LookupResponse(callsign=[Field("call", "AA7BQ"), Field("xref", "W1AW"), Field("fname", "Hiram")])
    apply_lookup(other, xref)
    assert other.name == "Hiram"

    other.comment = "Zürich"
    assert b"<comment:7>" + "Zürich".encode() in adif_record(other)


def test_prepare_qso_uses_lookup_and_rejects_a_bad_frequency():
    when = datetime(2026, 10, 18, 23, 4, 5, tzinfo=timezone.utc)
    lookup = LookupResponse(
        callsign=[Field("call", "AA7BQ"), Field("fname", "Fred"), Field("grid", "DM32af")]
    )
    qso = prepare_qso(
        call="aa7bq",
        station_call="KC2RGW",
        logbook_key="secret",
        tx_mhz="14.195",
        rx_mhz="14.200",
        mode_name="USB",
        rst_sent="59",
        rst_rcvd="57",
        power="100",
        comment="",
        lookup=lookup,
        time_on=when,
    )
    record = adif_record(qso)
    assert b"<call:5>AA7BQ" in record
    assert b"<band:3>20m" in record
    assert b"<submode:3>USB" in record
    assert b"<gridsquare:6>DM32af" in record
    assert b"<name:4>Fred" in record

    try:
        prepare_qso(
            call="W1AW",
            station_call="KC2RGW",
            logbook_key="secret",
            tx_mhz="9.5",
            mode_name="USB",
        )
    except LogInputError as exc:
        assert "outside the amateur bands" in str(exc)
    else:
        raise AssertionError("expected LogInputError")


def _record(**fields: str) -> LookupResponse:
    return LookupResponse(callsign=[Field(name, value) for name, value in fields.items()])


def test_groups_fields_and_drops_name_parts():
    record = _record(call="AA7BQ", fname="Fred", name="Lloyd", grid="DM32af", lotw="1", newthing="x")
    groups = grouped_fields(record)
    assert [title for title, _items in groups] == ["Operator", "On the air", "QSL", "Other"]
    operator = [(item.name, item.value) for item in groups[0][1]]
    assert operator == [("call", "AA7BQ"), ("name_fmt", "Fred Lloyd")]
    assert full_name(record) == "Fred Lloyd"
    club = grouped_fields(_record(call="W1AW", name_fmt="ARRL HQ OPERATORS CLUB", trustee="AF5ID"))
    assert [(item.name, field_label(item.name)) for item in club[0][1]][-1] == ("trustee", "Trustee")
    assert full_name(_record(name_fmt='Fred "FL" Lloyd', fname="Fred")) == 'Fred "FL" Lloyd'


def test_split_groups_cuts_at_a_group_edge():
    groups = [("A", [Field("a", "1")] * 5), ("B", [Field("b", "1")] * 2), ("C", [Field("c", "1")] * 4)]
    left, right = split_groups(groups)
    assert (len(left), len(right)) == (5, 6)
    assert split_groups([]) == ([], [])


def test_qth_line():
    record = _record(addr2="Concord", state="NH", zip="03301", county="Merrimack", country="United States")
    assert qth_line(record) == "Concord, NH 03301 · Merrimack County · United States"
    assert qth_line(_record(country="Japan")) == "Japan"


def test_grid_center_and_path():
    lat, lon = grid_center("FN43cb")
    assert lat == 43.0625 and abs(lon - (-72 + 5 / 24)) < 1e-9
    assert grid_center("FN43") == (43.5, -71.0)
    assert grid_center("ZZ99") is None
    assert grid_center("FN4") is None
    tokyo = _record(lat="35.694", lon="139.770")
    assert path_from("FN43cb", tokyo).endswith("at 335°")
    assert path_from("FN43cb", tokyo).startswith("10,69")
    assert path_from("", tokyo) == ""
    assert path_from("FN43cb", _record(grid="FN43cb")).startswith("0 km")


def test_local_time():
    summer = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    winter = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    eastern = _record(TimeZone="Eastern", GMTOffset="-5", DST="Y")
    assert local_time(eastern, summer) == "08:00 (UTC−4)"
    assert local_time(eastern, winter) == "07:00 (UTC−5)"
    assert local_time(_record(GMTOffset="9", DST="N"), summer) == "21:00 (UTC+9)"
    assert local_time(_record(GMTOffset="5.5", DST="N"), summer) == "17:30 (UTC+5:30)"
    assert local_time(_record(GMTOffset="1", DST="Y"), summer) == "13:00 (UTC+1, standard time)"
    assert local_time(_record(), summer) == ""

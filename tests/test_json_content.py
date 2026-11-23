"""Executable format rules and fixed vectors for JSON content identity."""

from __future__ import annotations

import json
import math
import random
import unittest
from decimal import Decimal, FloatOperation, localcontext
from itertools import product

from chongming.contracts.json_codec import (
    MAX_SAFE_INTEGER,
    JsonValue,
    canonical_json,
    content_sha256,
    parse_json,
)


class JsonContentTests(unittest.TestCase):
    def test_fixed_canonical_bytes_and_sha256_vectors(self) -> None:
        vectors = (
            ("null", "null", "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"),
            ("-0.0", "0", "5feceb66ffc86f38d952786c6d696c79c2dbc239dd4e91b46729d73a27fb57e9"),
            ("[]", "[]", "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
            (
                '{ "heights_m": [[0.0, 1.25], [2e0, 3.50]] }',
                '{"heights_m":[[0,1.25],[2,3.5]]}',
                "db45136ecd257221c806c44b01c7bc15ee4716847239eeaf0e42318c672522d2",
            ),
            (
                r'{"z":1e0,"a":[true,null,"\u91cd\u660e",0.000001,-0]}',
                '{"a":[true,null,"重明",1e-06,0],"z":1}',
                "505cee27c76c18a7c98fccc3fbcdf5e1ee98af394b8e06d7b2a5fbf7c5810872",
            ),
        )
        for text, canonical, digest in vectors:
            with self.subTest(text=text):
                value = parse_json(text)
                self.assertEqual(canonical_json(value), canonical.encode("utf-8"))
                self.assertEqual(content_sha256(value), digest)
                self.assertEqual(content_sha256(parse_json(text.encode("utf-8"))), digest)

    def test_indentation_line_endings_key_order_and_unicode_escaping_do_not_matter(self) -> None:
        document: JsonValue = {
            "z": [1.0, -0.0, 0.000001, "重明\n /"],
            "a": {"second": False, "first": None},
        }
        digest = content_sha256(document)
        for ensure_ascii, sort_keys, indent in product(
            (False, True), (False, True), (None, 2, "\t")
        ):
            with self.subTest(ensure_ascii=ensure_ascii, sort_keys=sort_keys, indent=indent):
                text = json.dumps(
                    document, ensure_ascii=ensure_ascii, sort_keys=sort_keys, indent=indent
                )
                text = " \r\n" + text.replace("\n", "\r\n") + "\t \n"
                self.assertEqual(content_sha256(parse_json(text)), digest)

    def test_equal_numeric_spellings_have_one_identity(self) -> None:
        groups = (
            ("0", "-0", "0.0", "-0.0", "0e1000", "-0.000e-1000"),
            ("1", "1.0", "1e0", "0.1e1", "10e-1", "1.000000000000000000000000"),
            ("-12.5", "-12.50", "-125e-1", "-0.125e2"),
            ("0.000001", "1e-6", "1.0000E-006", "0.01e-4"),
            ("9007199254740991", "9007199254740991.0", "9.007199254740991e15"),
        )
        for spellings in groups:
            with self.subTest(spellings=spellings):
                self.assertEqual(len({content_sha256(parse_json(text)) for text in spellings}), 1)

    def test_numeric_normalization_preserves_boolean_types(self) -> None:
        value = parse_json("[false,0,-0.0,true,1,1.0,null]")
        self.assertEqual(canonical_json(value), b"[false,0,0,true,1,1,null]")
        self.assertNotEqual(content_sha256(True), content_sha256(1))
        self.assertNotEqual(content_sha256(False), content_sha256(0))
        self.assertIs(type(parse_json("1.0")), int)
        self.assertIs(type(parse_json("true")), bool)

    def test_safe_integer_limits_apply_to_all_input_notations(self) -> None:
        for sign in ("", "-"):
            token = sign + str(MAX_SAFE_INTEGER)
            with self.subTest(token=token):
                self.assertEqual(canonical_json(parse_json(token)), token.encode())
                self.assertEqual(canonical_json(parse_json(token + ".0")), token.encode())
        for token in (
            "9007199254740992",
            "9007199254740992.0",
            "9.007199254740992e15",
            "-9007199254740992",
            "9007199254740993",
            "1e20",
            "100000000000000000000",
        ):
            with self.subTest(token=token), self.assertRaises(ValueError):
                parse_json(token)
        for value in (
            MAX_SAFE_INTEGER + 1,
            -MAX_SAFE_INTEGER - 1,
            float(MAX_SAFE_INTEGER + 1),
            1e20,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_json(value)

    def test_supported_fractional_values_keep_their_precision(self) -> None:
        for token in (
            "0.1",
            "0.1000000000000000000000000",
            "0.30000000000000004",
            "1.2345678901234567",
            "13.723456789012346",
            "51.123456789012344",
            "1e-300",
            "5e-324",
            "-5e-324",
        ):
            with self.subTest(token=token):
                self.assertEqual(parse_json(token), float(token))
                canonical = canonical_json(parse_json(token))
                self.assertEqual(canonical_json(parse_json(canonical)), canonical)
        coordinate = 13.723456789012346
        self.assertNotEqual(
            content_sha256([coordinate]), content_sha256([math.nextafter(coordinate, math.inf)])
        )

    def test_unsupported_precision_overflow_and_underflow_are_rejected(self) -> None:
        for token in (
            "0.10000000000000001",
            "1.0000000000000001",
            "1.23456789012345678",
            "13.723456789012345",
            "9007199254740990.5",
            "1e309",
            "-1e309",
            "1e-324",
            "-1e-324",
            "4e-324",
            "0e9999999999999999999999999999999999",
        ):
            with self.subTest(token=token), self.assertRaises(ValueError):
                parse_json('{"nested":[' + token + "]}")

    def test_decimal_context_cannot_round_content_or_change_identity(self) -> None:
        text = "[1.2345678901234567,9007199254740991,1e-300]"
        expected = canonical_json(parse_json(text))
        with localcontext() as context:
            context.prec = 1
            context.Emax = 1
            context.Emin = -1
            context.traps[FloatOperation] = True
            self.assertEqual(canonical_json(parse_json(text)), expected)
            with self.assertRaises(ValueError):
                parse_json("1.23456789012345678")

    def test_actual_values_array_order_and_string_contents_change_identity(self) -> None:
        documents = (
            '{"a":1}',
            '{"a":true}',
            '{"a":"1"}',
            '{"a":null}',
            '{"a":0}',
            '{"a":-1}',
            '{"A":1}',
            '{"b":1}',
            '{"a":[1,2]}',
            '{"a":[2,1]}',
            '{"a":[1,1]}',
            '{"a":[1]}',
            '{"a":{"x":1}}',
            '{"a":{"x":2}}',
            '"ridge"',
            '" ridge"',
            '"ridge "',
            '"RIDGE"',
        )
        self.assertEqual(
            len({content_sha256(parse_json(text)) for text in documents}), len(documents)
        )

    def test_unicode_is_not_normalized_and_keys_sort_by_code_point(self) -> None:
        self.assertNotEqual(content_sha256("é"), content_sha256("e\u0301"))
        self.assertEqual(parse_json(r'"\ud83d\ude00"'), "😀")
        self.assertEqual(content_sha256(parse_json(r'"\ud83d\ude00"')), content_sha256("😀"))
        self.assertEqual(
            canonical_json({"\U00010000": "astral", "\ue000": "bmp", "a": 1}),
            '{"a":1,"\ue000":"bmp","\U00010000":"astral"}'.encode("utf-8"),
        )

    def test_json_string_escaping_has_one_canonical_form(self) -> None:
        value = parse_json(
            r'{"text":"quote: \" slash: \/ backslash: \\ controls: \b\f\n\r\t\u0000"}'
        )
        self.assertEqual(
            canonical_json(value),
            rb'{"text":"quote: \" slash: / backslash: \\ controls: \b\f\n\r\t\u0000"}',
        )

    def test_duplicate_keys_are_rejected_after_escape_decoding_at_every_depth(self) -> None:
        for text in (
            '{"a":1,"a":2}',
            '{"a":1,"a":1}',
            '{"a":null,"a":false}',
            r'{"a":1,"\u0061":2}',
            r'{"重明":1,"\u91cd\u660e":2}',
            '{"nested":[{"a":1,"a":2}]}',
        ):
            with (
                self.subTest(text=text),
                self.assertRaisesRegex(ValueError, "duplicate JSON object key"),
            ):
                parse_json(text)

    def test_nonfinite_numbers_are_rejected_in_text_and_native_values(self) -> None:
        for text in ("NaN", "Infinity", "-Infinity", "[NaN]", '{"value":Infinity}'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "non-finite"):
                parse_json(text)
        for number in (math.nan, math.inf, -math.inf):
            with self.subTest(number=number), self.assertRaisesRegex(ValueError, "finite"):
                canonical_json({"nested": [number]})

    def test_only_utf8_without_a_bom_is_accepted(self) -> None:
        self.assertEqual(parse_json('"重明"'.encode()), "重明")
        for content in (
            b'"\xff"',
            b"\xef\xbb\xbf{}",
            "\ufeff{}",
            '"重明"'.encode("utf-16"),
            '"重明"'.encode("utf-16-le"),
            '"重明"'.encode("utf-32"),
            b'"\xed\xa0\x80"',
        ):
            with self.subTest(content=content), self.assertRaises(ValueError):
                parse_json(content)

    def test_lone_surrogates_are_rejected_in_keys_and_values(self) -> None:
        for text in (r'"\ud800"', r'"\udfff"', r'"\udc00\ud800"', r'{"\ud800":1}', r'["\udfff"]'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_json(text)
        for value in ("\ud800", {"\udfff": None}, ["\ud800"]):
            with self.subTest(value=repr(value)):
                self.assertRaises(ValueError, canonical_json, value)
                self.assertRaises(ValueError, content_sha256, value)

    def test_malformed_json_is_rejected(self) -> None:
        for text in (
            "",
            " ",
            "null null",
            "01",
            "+1",
            ".1",
            "[1,]",
            '{"a":1,}',
            "{'a':1}",
            '{1:"a"}',
            "undefined",
            "{} // comment",
            '"raw\nnewline"',
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_json(text)

    def test_non_json_python_values_and_non_string_keys_are_rejected(self) -> None:
        for value in (
            b"null",
            (),
            set(),
            Decimal("0.1"),
            object(),
            {1: "one"},
            {None: 1},
            {True: 1},
        ):
            with self.subTest(value=repr(value)):
                self.assertRaises(TypeError, canonical_json, value)
                self.assertRaises(TypeError, content_sha256, value)
        invalid_contents: tuple[object, ...] = (None, 1, {}, [], bytearray(b"{}"))
        for content in invalid_contents:
            with self.subTest(content=repr(content)):
                self.assertRaises(TypeError, parse_json, content)

    def test_canonicalization_does_not_mutate_native_input(self) -> None:
        document: JsonValue = {"z": [1.0, -0.0, 1.25], "a": {"second": 2, "first": 1}}
        before = json.dumps(document)
        canonical_json(document)
        content_sha256(document)
        self.assertEqual(json.dumps(document), before)

    def test_canonicalization_is_idempotent_for_deterministic_generated_values(self) -> None:
        random_source = random.Random(42)
        values: JsonValue = [random_source.uniform(-1e6, 1e6) for _ in range(128)]
        canonical = canonical_json(values)
        for _ in range(3):
            value = parse_json(canonical)
            self.assertEqual(canonical_json(value), canonical)
            self.assertEqual(content_sha256(value), content_sha256(values))


if __name__ == "__main__":
    unittest.main()

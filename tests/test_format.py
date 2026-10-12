"""On-disk encodings: every torn, truncated or flipped document is detected."""
import unittest

from safewrite.format import (IntegrityError, decode_doc, encode_doc, key_to_name, name_to_key)


class DocFormat(unittest.TestCase):
    def test_roundtrip_sizes(self):
        for n in (0, 1, 23, 24, 511, 512, 513, 4096, 1 << 20):
            data = bytes((i * 31 + 7) % 256 for i in range(n))
            self.assertEqual(decode_doc(encode_doc(data)), data)

    def test_every_single_byte_flip_is_detected(self):
        data = b"item,qty\nwater,120\nfood,80\n" * 5
        rec = encode_doc(data)
        for i in range(len(rec)):
            bad = bytearray(rec)
            bad[i] ^= 0x01
            with self.assertRaises(IntegrityError, msg=f"flip at byte {i} went unnoticed"):
                decode_doc(bytes(bad))

    def test_every_truncation_and_extension_is_detected(self):
        rec = encode_doc(b"hello world" * 40)
        for cut in range(len(rec)):
            with self.assertRaises(IntegrityError):
                decode_doc(rec[:cut])
        with self.assertRaises(IntegrityError):
            decode_doc(rec + b"\0")
        with self.assertRaises(IntegrityError):
            decode_doc(None)

    def test_zero_filled_file_is_rejected(self):
        with self.assertRaises(IntegrityError):
            decode_doc(b"\0" * 4096)


class KeyNames(unittest.TestCase):
    def test_roundtrip_and_safety(self):
        for key in ["inventory.csv", "Report.MD", "a/b", "..", ".hidden", "ünïcødé 文档", "x~y", "%41", "a b"]:
            name = key_to_name(key)
            self.assertNotIn("/", name)
            self.assertNotIn("~", name)
            self.assertNotEqual(name, "..")
            self.assertFalse(name.startswith("."))
            self.assertEqual(name_to_key(name), key)

    def test_case_insensitive_filesystems_cannot_collide(self):
        self.assertNotEqual(key_to_name("A").lower(), key_to_name("a").lower())

    def test_temp_names_are_not_keys(self):
        self.assertIsNone(name_to_key("inventory.csv~3.tmp"))

    def test_bad_keys(self):
        for key in ["", "x" * 300]:
            with self.assertRaises(ValueError):
                key_to_name(key)


if __name__ == "__main__":
    unittest.main()

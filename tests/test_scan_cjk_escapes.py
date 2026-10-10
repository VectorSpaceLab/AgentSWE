"""tools/scan.py counts Han characters written as escapes as CJK, the same as raw characters (an Editing test once
asserted an escaped Chinese sentence that the raw-character rule could not see). The fixtures are assembled at run
time so that this file itself scans clean."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import scan  # noqa: E402

BS = "\\"
HAN = chr(0x6BCF)


def lines(text):
    return [text.count("\n", 0, pos) + 1 for pos in scan.cjk_hits(text)]


class CjkEscapes(unittest.TestCase):
    def test_raw_and_escaped_han_are_found(self):
        text = "\n".join([f"raw = \"{HAN}\"", f"py = \"{BS}u6bcf case\"", f"wide = \"{BS}U00004e00\"",
                          "hex = \"&#" + "x4E00;\"", "dec = \"&#" + "19968;\""]) + "\n"
        self.assertEqual(lines(text), [1, 2, 3, 4, 5])

    def test_other_escapes_are_ignored(self):
        text = f"a = \"{BS}u0041{BS}u00e9{BS}u2014\"\nb = \"&#x41;&#233;\"\n"
        self.assertEqual(lines(text), [])

    def test_one_hit_per_line(self):
        self.assertEqual(lines(f"{BS}u6bcf{HAN}{BS}u4e00\n"), [1])


if __name__ == "__main__":
    unittest.main()

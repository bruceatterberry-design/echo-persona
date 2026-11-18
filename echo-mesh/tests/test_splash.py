import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    import PIL  # noqa: F401
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False
from mesh import agreement


@unittest.skipUnless(HAVE_PIL, "needs Pillow")
class Splash(unittest.TestCase):
    def test_full_screen_splash_draws_and_has_the_brand(self):
        import tempfile
        from mesh.dashboard import PilCtx
        from mesh.splash import draw_splash, TAGLINE
        c = PilCtx(size=(1920, 1080)); draw_splash(c, 1920, 1080)
        p = tempfile.mkdtemp() + "/s.png"; c.save(p)
        from PIL import Image
        im = Image.open(p); self.assertEqual(im.size, (1920, 1080))
        self.assertEqual(TAGLINE, "Distributed AI. Infinite Possibilities.")
        self.assertNotEqual(im.getpixel((960, 390)), im.getpixel((10, 10)))          # something bright is drawn at the ring


class Legal(unittest.TestCase):
    def test_terms_name_the_publisher_and_limit_liability(self):
        t = agreement.TERMS
        self.assertIn("bruceatterberry-design", t); self.assertIn("AS IS", t); self.assertIn("not liable", t)
        self.assertTrue(agreement.TERMS_VERSION)
        self.assertNotIn("Alex", t); self.assertNotIn("Springfield", t)                    # no personal details in a text that ships in the .exe


if __name__ == "__main__":
    unittest.main()

import unittest

from scraper import (
    best_channel_name,
    clean_name,
    is_generic_channel_name,
    name_from_url,
    normalize_url,
    render_category_playlists,
    render_root_m3u,
    slugify,
    title_case_name,
    _category_label_from_filename,
)


class TestHelpers(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(
            normalize_url("//cdn.example/x.m3u8", "https://site.example/a"),
            "https://cdn.example/x.m3u8",
        )
        self.assertEqual(
            normalize_url("/path/stream.m3u8", "https://site.example/a"),
            "https://site.example/path/stream.m3u8",
        )
        self.assertIsNone(normalize_url("javascript:alert(1)", "https://x"))

    def test_clean_name(self):
        self.assertEqual(clean_name(" Assistir SBT - Olhos na TV "), "SBT")
        self.assertEqual(clean_name("Ver Globo | Olhos na TV"), "Globo")

    def test_title_case(self):
        self.assertEqual(title_case_name("rede brasil"), "Rede Brasil")
        self.assertEqual(title_case_name("SBT"), "SBT")
        self.assertEqual(title_case_name("canal de noticias"), "Canal de Noticias")

    def test_channel_name_fallback(self):
        self.assertTrue(is_generic_channel_name("TV Online Grátis"))
        self.assertEqual(
            name_from_url("https://www.olhosnatv.com.br/2026/09/rede-brasil.html"),
            "rede brasil",
        )
        self.assertEqual(
            best_channel_name(
                ["TV Online Grátis"],
                "https://www.olhosnatv.com.br/2026/09/rede-brasil.html",
            ),
            "Rede Brasil",
        )

    def test_slugify(self):
        self.assertEqual(slugify("TVs Abertas"), "tvs-abertas")
        self.assertEqual(slugify("Notícias do Mundo"), "noticias-do-mundo")

    def test_ssiptv_nested_playlists(self):
        state = {
            "channels": [
                {
                    "id": "abc",
                    "name": "SBT",
                    "logo": "https://x/logo.png",
                    "categories": ["TVs Abertas", "Variedades"],
                    "stream_url": "https://x/live.m3u8",
                }
            ]
        }
        cfg = {"github_raw_base": "https://raw.githubusercontent.com/josemtocco/olhosnatv-m3u/main"}
        files = render_category_playlists(state, cfg)
        self.assertEqual(set(files), {"categoria-tvs-abertas.m3u", "categoria-variedades.m3u"})
        self.assertIn('group-title="TVs Abertas"', files["categoria-tvs-abertas.m3u"])
        root = render_root_m3u(state, cfg, files)
        self.assertEqual(root.count('type="playlist"'), 2)
        self.assertIn(
            "https://raw.githubusercontent.com/josemtocco/olhosnatv-m3u/main/categoria-tvs-abertas.m3u",
            root,
        )
        self.assertIn(
            "https://raw.githubusercontent.com/josemtocco/olhosnatv-m3u/main/categoria-variedades.m3u",
            root,
        )
        self.assertIn("TVs Abertas", root)
        self.assertIn("Variedades", root)

    def test_category_label_recovery(self):
        state = {
            "channels": [
                {"categories": ["TVs Abertas"], "name": "X", "id": "1", "stream_url": "u"}
            ]
        }
        self.assertEqual(
            _category_label_from_filename("categoria-tvs-abertas.m3u", state),
            "TVs Abertas",
        )


if __name__ == "__main__":
    unittest.main()

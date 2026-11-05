import unittest
from types import SimpleNamespace

from cogs.lfg import (
    FLUXER_MESSAGE_COMPONENTS_ENABLED,
    LfgCog,
    _future_fluxer_link_components,
)


class PresenceLinkTests(unittest.TestCase):
    def _cog(self):
        cog = LfgCog.__new__(LfgCog)
        cog.bot = SimpleNamespace()
        return cog

    def test_fluxer_keeps_clickable_embed_links_while_components_are_disabled(self):
        self.assertFalse(FLUXER_MESSAGE_COMPONENTS_ENABLED)
        payload = {
            "fields": [
                {"name": "Steam", "value": "[Open store page](https://store.steampowered.com/app/1245620/)", "inline": True},
                {"name": "QuestLog", "value": "[View profile](https://questlog.casual-heroes.com/u/ryven/)", "inline": True},
            ],
            "buttons": [
                {"kind": "steam", "url": "https://store.steampowered.com/app/1245620/"},
                {"kind": "questlog", "url": "https://questlog.casual-heroes.com/u/ryven/"},
            ],
        }

        embed = self._cog()._build_embed(payload).to_dict()

        self.assertEqual([field["name"] for field in embed["fields"]], ["Steam", "QuestLog"])

    def test_future_components_are_allowlisted_and_ready_to_enable(self):
        rows = _future_fluxer_link_components({
            "buttons": [
                {"kind": "steam", "url": "https://store.steampowered.com/app/1245620/"},
                {"kind": "questlog", "url": "https://evil.example/u/ryven/"},
            ],
        })

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["components"][0]["label"], "Steam")
        self.assertEqual(len(rows[0]["components"]), 1)


if __name__ == "__main__":
    unittest.main()

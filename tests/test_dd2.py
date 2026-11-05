import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.dd2 import (
    _clean_embed_text, _combat_lines, _drop_lines, _parse_dd2_query,
    build_category_fallback, build_dd2_embed, build_dd2_help_embed,
    build_dd2_record_embed, DD2Cog,
)


class DD2EmbedTests(unittest.TestCase):
    def test_combat_and_drop_lines_preserve_extracted_values(self):
        data = {
            "combat": {
                "health_varies": True, "health_min": 1000,
                "health_max": 4200, "physical_defense": 53,
                "magick_defense": 48,
            },
            "weaknesses": ["Light"], "resistances": ["Dark"],
            "drops": [{"name": "Scrap Iron", "rate": 40, "quantity": 2}],
        }
        self.assertIn("1,000-4,200", "\n".join(_combat_lines(data)))
        self.assertIn("Weak to:** Light", "\n".join(_combat_lines(data)))
        self.assertEqual(_drop_lines(data), ["• Scrap Iron - 40% - ×2"])

    def test_embed_uses_live_map_image_and_record_link(self):
        data = {
            "name": "Goblin", "detail_url": "https://example.test/goblin",
            "map_url": "https://example.test/map", "map_image_url": "https://example.test/map.png",
            "placement_count": 7, "locations": ["Vermund"],
            "combat": {}, "drops": [], "steam_build_id": 25290080,
        }
        embed = build_dd2_embed(data)
        self.assertEqual(embed.url, data["detail_url"])
        self.assertEqual(embed.image["url"], data["map_image_url"])
        self.assertIn("7 verified map placements", embed.fields[0]["value"])
        self.assertEqual(embed.description, "Dragon's Dogma 2 encounter data.")
        self.assertEqual(embed.footer["text"], "QuestLog - Dragon's Dogma 2 Data")
        self.assertNotIn("25290080", embed.footer["text"])

    def test_category_parser_supports_new_and_legacy_forms(self):
        self.assertEqual(
            _parse_dd2_query("weapons   Almace"),
            ("weapons", "Almace"),
        )
        self.assertEqual(_parse_dd2_query("boss Cyclops"), ("boss", "Cyclops"))
        self.assertEqual(_parse_dd2_query("commands"), ("commands", ""))
        self.assertEqual(_parse_dd2_query("Goblin"), ("search", "Goblin"))
        self.assertEqual(
            _parse_dd2_query("Cloudfall Strike"),
            ("search", "Cloudfall Strike"),
        )

    def test_dd2_embed_text_uses_plain_dashes(self):
        self.assertEqual(
            _clean_embed_text("Skill — Rank 4 · 1,000 Dcp – ready"),
            "Skill - Rank 4 - 1,000 Dcp - ready",
        )

    def test_commands_embed_lists_every_supported_lookup(self):
        embed = build_dd2_help_embed()
        content = "\n".join(
            [embed.description]
            + [field["value"] for field in embed.fields]
        )
        for command in (
            "enemy", "boss", "weapons", "armor", "rings", "vocations",
            "quests", "skills", "items", "merchants", "pawn",
        ):
            self.assertIn(f"!dd2 {command}", content)
        self.assertIn("!dd2 locations <name>", content)
        self.assertNotIn("!locations", content)
        self.assertIn("!dd2 commands", content)
        self.assertIn("!dd2 <name>", content)
        self.assertIn("!dd2 cyclops", content)
        self.assertNotIn("—", content)
        self.assertNotIn("·", content)

    def test_equipment_embed_shows_image_stats_and_acquisition(self):
        data = {
            "name": "Almace",
            "description": "A frost-crusted sword.",
            "detail_url": "/soulslike/database/dragons-dogma-2/weapon/example/",
            "icon_url": "/api/soulslike/wiki/dragons_dogma_2/assets/icon/",
            "stats": ["Physical attack: 190", "Magick attack: 80"],
            "acquisition": ["Sold by Celeste · Restocks every 1 day"],
        }
        embed = build_dd2_record_embed(data, "weapons")
        self.assertIn("Almace", embed.title)
        self.assertIn("questlog.casual-heroes.com", embed.thumbnail["url"])
        fields = {field["name"]: field["value"] for field in embed.fields}
        self.assertIn("Physical attack: 190", fields["Stats"])
        self.assertIn("Sold by Celeste", fields["Where to find it"])

    def test_vocation_embed_shows_abilities_and_starting_gear(self):
        embed = build_dd2_record_embed({
            "name": "Archer",
            "description": "A ranged vocation.",
            "details": ["Available to Arisen and Pawns"],
            "weapon_skills": ["Barrage Shot — Rank 1 · No Dcp"],
            "maister_skills": [
                "Heavenly Shot — Maister Taliesin at Sacred Arbor · Complete both "
                "Archer quests, rest one day, and speak to him again · Use Hunter's Secret",
            ],
            "dark_arisen_skills": [
                "Cataract Shot — Find as a rolled skill on a Dark Arisen relic weapon",
            ],
            "core_skills": ["Leaping Punt — Rank 1 · 150 Dcp"],
            "augments": ["Ambuscade — Rank 2 · 300 Dcp"],
            "starting_equipment": ["Fluted Bow (Weapon)"],
        }, "vocations")
        fields = {field["name"]: field["value"] for field in embed.fields}
        self.assertIn("Barrage Shot", fields["Weapon skills"])
        self.assertIn("Taliesin", fields["Maister skills"])
        self.assertIn("relic weapon", fields["Dark Arisen skills"])
        self.assertIn("Leaping Punt", fields["Core skills"])
        self.assertIn("Ambuscade", fields["Augments"])
        self.assertIn("Fluted Bow", fields["Starting equipment"])
        self.assertNotIn("—", str(embed.to_dict()))
        self.assertNotIn("·", str(embed.to_dict()))

    def test_quest_embed_only_uses_spoiler_safe_payload(self):
        embed = build_dd2_record_embed({
            "name": "Monster Culling",
            "description": "Help secure the roads.",
            "objectives": ["Investigate the monster threats."],
            "detail_url": "https://example.test/quest/",
        }, "quests")
        fields = {field["name"]: field["value"] for field in embed.fields}
        self.assertIn("Investigate", fields["Objective"])
        self.assertNotIn("reward", str(embed.to_dict()).casefold())
        self.assertNotIn("completion", str(embed.to_dict()).casefold())

    def test_location_fallback_links_to_locations_page(self):
        embed = build_category_fallback("locations")
        self.assertIn("!dd2 locations <name>", embed.description)
        self.assertIn("/location/", embed.description)

    def test_location_embed_uses_concise_section_labels(self):
        embed = build_dd2_record_embed({
            "name": "Harve Village",
            "description": "Harve Village is marked on the world map.",
            "monsters": ["Saurian"],
            "loot": ["Mighty Roborant"],
            "npcs": ["Abel", "Aizam", "+ 19 more on QuestLog"],
        }, "locations")
        fields = {field["name"]: field["value"] for field in embed.fields}
        self.assertIn("Saurian", fields["Nearby enemies"])
        self.assertIn("Mighty Roborant", fields["Nearby treasure"])
        self.assertNotIn("placement", fields["NPCs"])

    def test_location_embed_preserves_complete_canonical_map_state(self):
        map_url = (
            "https://questlog.casual-heroes.com/soulslike/database/"
            "dragons-dogma-2/map/?layer=base&x=3655.000&y=3483.000&zoom=0.5"
            "&region=dd2%3Aplayer_area%3A210%2Cdd2%3Aplayer_area%3A214"
            "&marker_id=dd2%3Amap_marker%3Abase%3Amapicondata%3A26"
            "&kinds=enemy%2Cnpc%2Cloot%2Cpoi"
        )
        embed = build_dd2_record_embed({
            "name": "Vernworth",
            "description": "Vernworth is marked on the world map.",
            "map_url": map_url,
        }, "locations")

        fields = {field["name"]: field["value"] for field in embed.fields}
        self.assertEqual(embed.url, map_url)
        self.assertIn(map_url, fields["Map"])
        self.assertIn("region=dd2%3Aplayer_area%3A210", fields["Map"])
        self.assertIn("marker_id=dd2%3Amap_marker%3Abase", fields["Map"])
        self.assertIn("kinds=enemy%2Cnpc%2Cloot%2Cpoi", fields["Map"])


class DD2SearchFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_universal_search_routes_to_resolved_category(self):
        cog = object.__new__(DD2Cog)
        cog._request = AsyncMock(return_value=(200, {
            "category": "weapons", "name": "Trusty Sword",
        }))
        cog._lookup = AsyncMock()
        ctx = SimpleNamespace(reply=AsyncMock())

        await cog._search(ctx, "Trusty Sword")

        cog._lookup.assert_awaited_once_with(ctx, "weapons", "Trusty Sword")
        ctx.reply.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.elden_ring import (
    EldenRingCog, _clean_embed_text, _parse_query, build_category_fallback,
    build_help_embed, build_record_embed, categories_for,
)


class EldenRingEmbedTests(unittest.TestCase):
    def test_reforged_command_is_matched_before_elden_ring_command(self):
        cog = EldenRingCog(SimpleNamespace())
        content = "err flamelost greatblades"
        matched = next(name for name in cog._commands if content.startswith(name))

        self.assertEqual(matched, "err")

    def test_parser_supports_universal_and_filtered_searches(self):
        self.assertEqual(_parse_query("er", "Uchigatana"), ("search", "Uchigatana"))
        self.assertEqual(_parse_query("er", "weapon Uchigatana"), ("weapons", "Uchigatana"))
        self.assertEqual(_parse_query("err", "fortune Crucible"), ("fortunes", "Crucible"))
        self.assertEqual(_parse_query("er", "commands"), ("commands", ""))

    def test_vanilla_does_not_advertise_reforged_only_categories(self):
        self.assertNotIn("fortunes", categories_for("er"))
        self.assertIn("fortunes", categories_for("err"))
        self.assertEqual(_parse_query("er", "fortune Crucible"), ("search", "fortune Crucible"))

    def test_help_embeds_show_universal_filters_and_game_prefix(self):
        er = build_help_embed("er")
        err = build_help_embed("err")
        er_text = str(er.to_dict())
        err_text = str(err.to_dict())
        for command in ("weapons", "armor", "talismans", "affinities", "spells", "ashes", "classes", "spirits", "tears", "bosses", "locations"):
            self.assertIn(f"!er {command}", er_text)
        self.assertIn("!er <name>", er_text)
        self.assertIn("!err <name>", err_text)
        self.assertIn("!err fortunes", err_text)
        self.assertIn("!err difficulty", err_text)
        self.assertNotIn("!locations", er_text)

    def test_weapon_embed_has_image_stats_skill_and_live_map(self):
        record = {
            "name": "Uchigatana", "type": "Katana", "weight": 5.5,
            "damage": {"phy": 115, "mag": 0},
            "requirements": {"str": 11, "dex": 15},
            "scaling": {"str": "D", "dex": "D"},
            "special": "Unsheathe", "is_somber": False, "max_upgrade": 25,
            "image_url": "/api/soulslike/site-media/elden_ring/icon.webp/",
        }
        embed = build_record_embed("er", "weapons", record, [{
            "description": "Deathtouched Catacombs", "map_url": "https://example.test/map",
        }])
        fields = {field["name"]: field["value"] for field in embed.fields}
        self.assertIn("Unsheathe", fields["Details"])
        self.assertIn("PHY 115", fields["Attack"])
        self.assertIn("Deathtouched Catacombs", fields["Where to find it"])
        self.assertIn("site-media", embed.thumbnail["url"])

    def test_location_embed_shows_loot_and_map(self):
        embed = build_record_embed("err", "locations", {
            "name": "Church of Elleh", "description": "Limgrave",
            "kind": "grace", "map_url": "https://example.test/map",
            "loot": [{"name": "Smithing Stone"}],
        })
        fields = {field["name"]: field["value"] for field in embed.fields}
        self.assertIn("Smithing Stone", fields["Loot"])
        self.assertIn("Open this location", fields["Map"])

    def test_boss_embed_separates_rewards_from_location(self):
        embed = build_record_embed("er", "bosses", {
            "name": "Adan, Thief of Fire", "region": "Liurnia of the Lakes",
            "location": "Malefactor's Evergaol", "tier": "enemy",
            "rewards": ["3,800 runes", "Flame of the Fell God"],
            "image_url": "/wrong/remembrance.webp",
        }, [{
            "kind": "grace", "description": "Nearby grace",
            "map_url": "https://example.test/grace",
        }, {
            "kind": "boss", "description": "3,800 runes",
            "map_url": "https://example.test/boss",
        }])
        fields = {field["name"]: field["value"] for field in embed.fields}
        self.assertIn("Malefactor's Evergaol", fields["Encounter"])
        self.assertIn("3,800 runes", fields["Rewards"])
        self.assertIn("Flame of the Fell God", fields["Rewards"])
        self.assertNotIn("Where to find it", fields)
        self.assertIn("Open encounter", fields["Map"])
        self.assertIn("example.test/boss", fields["Map"])
        self.assertNotIn("example.test/grace", fields["Map"])
        self.assertIn("c50ea0523444ce124ad4", embed.thumbnail["url"])
        self.assertNotIn("remembrance", embed.thumbnail["url"])

    def test_reforged_fortune_embed_shows_unlock(self):
        embed = build_record_embed("err", "fortunes", {
            "name": "Example Fortune", "description": "Changes the journey.",
            "buffs": ["More health"], "drawbacks": ["Less stamina"],
            "how_to_unlock": "Defeat the example boss.",
        })
        fields = {field["name"]: field["value"] for field in embed.fields}
        self.assertIn("More health", fields["Benefits"])
        self.assertIn("Defeat", fields["How to unlock"])

    def test_embed_text_uses_plain_dashes(self):
        self.assertEqual(_clean_embed_text("Skill — 10 FP · ready – now"), "Skill - 10 FP - ready - now")
        embed = build_record_embed("er", "tears", {"name": "Test", "effect": "Power — high · brief"})
        self.assertNotIn("—", str(embed.to_dict()))
        self.assertNotIn("·", str(embed.to_dict()))

    def test_category_fallback_links_to_correct_game(self):
        self.assertIn("elden-ring/map", build_category_fallback("er", "locations").description)
        self.assertIn("elden-ring-reforged", build_category_fallback("err", "weapons").description)


class EldenRingFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_requests_remain_scoped_to_the_selected_game(self):
        cog = EldenRingCog(SimpleNamespace())
        cog._request = AsyncMock(return_value=(200, {"weapons": []}))

        await cog._catalog("er", "weapons")
        await cog._catalog("err", "weapons")

        calls = cog._request.await_args_list
        self.assertEqual(calls[0].args[0], "api/soulslike/weapons/")
        self.assertEqual(calls[0].args[1]["game"], "elden_ring")
        self.assertEqual(calls[1].args[0], "api/soulslike/weapons/")
        self.assertEqual(calls[1].args[1]["game"], "err")

    async def test_flamelost_only_resolves_in_reforged_catalog(self):
        cog = object.__new__(EldenRingCog)
        reforged_weapon = {"name": "Flamelost Greatblades"}
        cog._catalog = AsyncMock(side_effect=lambda game, category: (
            [reforged_weapon] if game == "err" and category == "weapons" else []
        ))
        cog._placements = AsyncMock(return_value=[])
        cog._boss_rewards = AsyncMock(return_value=[])

        er_ctx = SimpleNamespace(reply=AsyncMock())
        err_ctx = SimpleNamespace(reply=AsyncMock())
        await cog._search(er_ctx, "er", "Flamelost Greatblades")
        await cog._search(err_ctx, "err", "Flamelost Greatblades")

        er_embed = er_ctx.reply.await_args.kwargs["embed"]
        err_embed = err_ctx.reply.await_args.kwargs["embed"]
        self.assertIn("No exact Elden Ring match", er_embed.description)
        self.assertEqual(err_embed.title, "⚔️ Flamelost Greatblades")
        self.assertIn("Elden Ring Reforged", err_embed.description)

    async def test_universal_exact_match_opens_record_and_enriches_map(self):
        cog = object.__new__(EldenRingCog)
        cog._catalog = AsyncMock(side_effect=lambda game, category: (
            [{"name": "Uchigatana", "type": "Katana"}] if category == "weapons" else []
        ))
        cog._placements = AsyncMock(return_value=[{"description": "Limgrave", "map_url": "https://example.test/map"}])
        cog._boss_rewards = AsyncMock(return_value=[])
        ctx = SimpleNamespace(reply=AsyncMock())

        await cog._search(ctx, "er", "Uchigatana")

        ctx.reply.assert_awaited_once()
        embed = ctx.reply.await_args.kwargs["embed"]
        self.assertIn("Uchigatana", embed.title)
        cog._placements.assert_awaited_once_with("er", "Uchigatana")

    async def test_filtered_lookup_returns_suggestions_for_ambiguous_query(self):
        cog = object.__new__(EldenRingCog)
        cog._catalog = AsyncMock(return_value=[
            {"name": "Carian Knight's Sword"}, {"name": "Carian Regal Scepter"},
        ])
        cog._results = AsyncMock()
        ctx = SimpleNamespace(reply=AsyncMock())

        await cog._lookup(ctx, "er", "weapons", "Carian")

        cog._results.assert_awaited_once()

    async def test_universal_unique_short_name_opens_titled_boss(self):
        cog = object.__new__(EldenRingCog)
        cog._catalog = AsyncMock(side_effect=lambda game, category: (
            [{"name": "Malenia, Blade of Miquella", "key": "malenia"}]
            if category == "bosses" else
            [{"name": "Hand of Malenia"}] if category == "weapons" else
            [{"name": "Malenia's Armor"}] if category == "armor" else []
        ))
        cog._placements = AsyncMock(return_value=[])
        cog._boss_rewards = AsyncMock(return_value=["480,000 runes"])
        ctx = SimpleNamespace(reply=AsyncMock())

        await cog._search(ctx, "er", "Malenia")

        embed = ctx.reply.await_args.kwargs["embed"]
        self.assertIn("Malenia, Blade of Miquella", embed.title)

    async def test_boss_rewards_use_runes_and_verified_nearby_event_reward(self):
        cog = object.__new__(EldenRingCog)
        cog._boss_drop_catalog = AsyncMock(return_value=[{
            "name": "Adan, Thief of Fire",
            "drops": ["3.800 Runes", "Flame of the Fell God"],
        }])
        cog._map_catalog = AsyncMock(return_value=[{
            "name": "Flame of the Fell God", "label": "Flame of the Fell God",
            "description": "Event reward", "layer": "lands_between",
            "x": 17, "y": 0,
        }])
        rewards = await cog._boss_rewards("er", {"name": "Adan, Thief of Fire"}, [{
            "description": "3,800 runes", "layer": "lands_between", "x": 0, "y": 0,
        }])

        self.assertEqual(rewards, ["3,800 runes", "Flame of the Fell God"])


if __name__ == "__main__":
    unittest.main()

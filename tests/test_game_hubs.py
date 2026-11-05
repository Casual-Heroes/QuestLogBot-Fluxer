import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import fluxer

from cogs.game_hubs import (
    ENS_ALIASES, ENS_CATEGORIES, GAMES, GameHubsCog, _ens_challenge_groups,
    _featured_housing_plots, _ffxiv_node_status,
    command_directory_embed,
    _ms2_weapon_upgrades_from_html, _parse, help_embed, record_embed,
)


class GameHubEmbedTests(unittest.TestCase):
    def test_every_requested_game_category_is_registered(self):
        self.assertEqual(set(GAMES["ms2"]["categories"]), {"weapons", "shells", "tarstones", "items", "bosses"})
        self.assertIn("boss_rush", GAMES["r2"]["categories"])
        self.assertIn("relic_fragments", GAMES["r2"]["categories"])
        self.assertEqual(set(GAMES["nr"]["categories"]), {"weapons", "armor", "rings", "quivers", "runes", "gems", "bosses", "world"})
        self.assertIn("companions", GAMES["cd"]["categories"])
        self.assertIn("world", GAMES["cd"]["categories"])

    def test_parser_supports_universal_filtered_and_commands(self):
        self.assertEqual(_parse("ms2", "Hadern"), ("search", "Hadern"))
        self.assertEqual(_parse("r2", "relic_fragments Mythic"), ("relic_fragments", "Mythic"))
        self.assertEqual(_parse("nr", "rune Blink"), ("runes", "Blink"))
        self.assertEqual(_parse("cd", "commands"), ("commands", ""))

    def test_help_embeds_list_each_games_categories(self):
        for game, meta in GAMES.items():
            text = str(help_embed(game).to_dict())
            self.assertIn(f"!{game} <name>", text)
            for category in meta["categories"]:
                self.assertIn(f"!{game} {category}", text)

    def test_command_directory_lists_every_game_prefix_and_fits_embed_limits(self):
        embed = command_directory_embed()
        payload = embed.to_dict()
        rendered = str(payload)
        for prefix in ("dd2", "er", "err", "ms2", "r2", "nr", "cd", "ff", "ens"):
            self.assertIn(f"!{prefix}", rendered)
        self.assertIn("!dd2 Cyclops", rendered)
        self.assertIn("!dd2 boss Cyclops", rendered)
        self.assertEqual([field["name"] for field in embed.fields], [
            "Game databases", "Live tools", "How to use", "More QuestLog commands",
        ])
        self.assertLessEqual(len(embed.fields), 25)
        self.assertTrue(all(len(field["value"]) <= 1024 for field in embed.fields))
        self.assertLessEqual(len(str(payload)), 6000)

    def test_generic_record_embed_shows_image_stats_and_rewards(self):
        embed = record_embed("r2", "bosses", {
            "name": "Example Boss", "world": "Yaesha", "zone": "Temple",
            "rewards": ["Example Ring"], "image_path": "/static/example.png",
            "stats": [{"label": "Health", "display_value": "1,000", "suffix": ""}],
        })
        fields = {field["name"]: field["value"] for field in embed.fields}
        self.assertIn("Yaesha", fields["Details"])
        self.assertIn("1,000", fields["Stats"])
        self.assertIn("Example Ring", fields["Rewards"])
        self.assertIn("example.png", embed.thumbnail["url"])

    def test_ms2_weapon_embed_links_detail_and_shows_progression(self):
        embed = record_embed("ms2", "weapons", {
            "name": "Axatana", "source_id": "Axatana", "gameplay_tag": "Weapon.Melee.Axatana",
            "max_level": 20, "status": "decoded", "calculation_ready": True,
            "enabled_metrics": [{
                "label": "Damage", "display_level_0": 1.0, "display_level_max": 2.5,
                "display_mode": "multiplier", "suffix": "x",
            }],
            "missing_metrics": [{"label": "Stagger"}],
            "compatible_upgrades": [{
                "name": "Grudge Stone",
                "description": "Your Weapon deals a guaranteed Critical Hit",
            }],
        })
        fields = {field["name"]: field["value"] for field in embed.fields}
        self.assertEqual(
            embed.url,
            "https://questlog.casual-heroes.com/soulslike/mortal-shell-2/weapons/axatana/?game=mortal_shell_2",
        )
        self.assertIn("Base (+0):** 1x", fields["Damage progression"])
        self.assertIn("Maximum (+20):** 2.5x", fields["Damage progression"])
        self.assertIn("Total increase:** 150%", fields["Damage progression"])
        self.assertIn("Grudge Stone", fields["Works with this weapon"])
        self.assertNotIn("Data status", str(embed.to_dict()))
        self.assertNotIn("Build calculations", str(embed.to_dict()))
        self.assertNotIn("Not yet decoded", fields)

    def test_ms2_weapon_page_parser_reads_player_facing_upgrade_cards(self):
        source = '''
        <p>Works with this weapon</p><h2>2 upgrade options</h2>
        <article><h3>Grudge Stone</h3><p>Your Weapon deals a guaranteed Critical Hit</p></article>
        <article><h3>Champion's Brooch</h3><p>Consume {R} Resolve to infuse your Weapon</p></article>
        </section>
        '''
        upgrades = _ms2_weapon_upgrades_from_html(source)
        self.assertEqual([row["name"] for row in upgrades], ["Grudge Stone", "Champion's Brooch"])
        self.assertIn("Resolve", upgrades[1]["description"])
        self.assertNotIn("{R}", upgrades[1]["description"])

    def test_ms2_shell_embed_shows_combat_profile_and_upgrade_tree(self):
        embed = record_embed("ms2", "shells", {
            "name": "Eredrim", "full_name": "Eredrim, The Venerable",
            "shell_health_display": "110", "shell_health_max_display": "459.8",
            "shell_point_capacity": 42, "memory_count": 9, "total_memory_cost": 4500,
            "combat_stats": [{"label": "Ability damage", "display": "50"}],
            "resistances": [{"label": "Bleed", "display": "10"}],
            "upgrades": [{
                "name": "Alacrity", "bond_tier_display": "I",
                "description": "Each level reduces Resolve cost",
            }],
        })
        fields = {field["name"]: field["value"] for field in embed.fields}
        self.assertIn("459.8", fields["Shell details"])
        self.assertIn("Ability damage - 50", fields["Abilities"])
        self.assertIn("Bleed - 10", fields["Resistances"])
        self.assertIn("Alacrity", fields["Bond Tier I upgrades"])

    def test_ms2_tarstone_and_boss_embeds_prioritize_gameplay_data(self):
        tarstone = record_embed("ms2", "tarstones", {
            "name": "Acolyte's Stone", "category": "melee", "slot": "weapon",
            "color": "purple", "max_level": 3,
            "description": "Consume {R} Resolve to perform an attack",
            "levels": [{"level": 1, "effect": "The attack gains 15% Critical Chance"}],
        })
        tarstone_fields = {field["name"]: field["value"] for field in tarstone.fields}
        self.assertIn("Level 1", tarstone_fields["Upgrade effects"])
        self.assertNotIn("{R}", tarstone.description)

        boss = record_embed("ms2", "bosses", {
            "name": "Droeg, the Conquerer", "source_id": "DwarfBoss",
            "region": "Conquered Temple", "location": "Conqueror's Hoard",
            "health_display": "6,900", "poise_display": "400",
            "lowest_resistance": {"label": "Shock", "display": "16 buildup"},
            "immune_statuses": ["Frost"], "gloom_reward": 29993,
            "gloom_reward_display": "29,993",
        })
        boss_fields = {field["name"]: field["value"] for field in boss.fields}
        self.assertIn("6,900", boss_fields["Encounter"])
        self.assertIn("Shock", boss_fields["Weaknesses and resistances"])
        self.assertIn("29,993 Gloom", boss_fields["Rewards"])
        self.assertTrue(boss.url.endswith("/bosses/DwarfBoss/"))

    def test_ms2_item_embed_shows_familiarity_and_acquisition(self):
        embed = record_embed("ms2", "items", {
            "name": "Bloodseed", "category": "familiarity-item", "rarity": "common",
            "description": "A useful seed.",
            "stages": [{
                "stage": 1, "found_count_required": 1,
                "active_text": "Cures Bloodcurse for 30s",
                "passive_text": "Grants 15% Bloodcurse mitigation",
            }],
            "acquisition_locations": [{
                "region": "Multiple regions", "landmark": "Revenant Graves",
                "route": "Harvest Bloodseed throughout the world.",
            }],
        })
        fields = {field["name"]: field["value"] for field in embed.fields}
        self.assertIn("Familiarity Item", fields["Item details"])
        self.assertIn("15% Bloodcurse mitigation", fields["Familiarity effects"])
        self.assertIn("Revenant Graves", fields["How to get it"])

    def test_nr_weapon_embed_uses_direct_item_page_and_weapon_data(self):
        embed = record_embed("nr", "weapons", {
            "id": "1985442680644053848", "name": "A Singular Purpose",
            "weapon_class": "Greatsword", "damage_school": "physical", "weight": 12,
            "default_runes": [{"name": "Whirlwind"}],
        })
        fields = {field["name"]: field["value"] for field in embed.fields}
        self.assertEqual(
            embed.url,
            "https://questlog.casual-heroes.com/soulslike/database/no-rest-for-the-wicked/items/a-singular-purpose-1985442680644053848/",
        )
        self.assertIn("Greatsword", fields["Weapon details"])
        self.assertIn("Whirlwind", fields["Default runes"])

    def test_cd_weapon_embed_shows_enhancement_sources_and_refinement(self):
        embed = record_embed("cd", "weapons", {
            "id": "antumbra", "name": "Antumbra's Eye", "equip_type": "Longbow",
            "rarity": "Legendary", "slots": [{"name": "Main Hand"}],
            "socket_enabled": True, "socket_count": 2,
            "stats_by_tier": [{"name": "+0", "stats": [{"name": "Attack", "display_value": "145"}]}],
            "passive_skills": [{"skill": "Moonlight", "level": 3}],
            "prices": [{"amount": 3330, "currency": "Copper"}, {"amount": 166, "currency": "Camp Weapons"}],
            "stores": [{"name": "Example Merchant", "refills": 1}],
            "recipes": [{
                "from": 0, "to": 1,
                "materials": [{"name": "Iron", "count": 2}],
                "prices": [{"amount": 3384, "currency": "Copper"}],
            }],
        })
        fields = {field["name"]: field["value"] for field in embed.fields}
        self.assertIn("Main Hand", fields["Weapon details"])
        self.assertIn("Attack - 145", fields["Stats at +0"])
        self.assertIn("Moonlight - Level 3", fields["Passive skills"])
        self.assertIn("3,330 Copper\n• 166 Camp Weapons", fields["Purchase price"])
        self.assertIn("Example Merchant", fields["Sold by"])
        self.assertIn("2× Iron", fields["Refinement +0 to +1"])
        self.assertIn("3,384 Copper", fields["Refinement +0 to +1"])

    def test_r2_weapon_archetype_and_boss_embeds_show_player_data(self):
        weapon = record_embed("r2", "weapons", {
            "name": "Abyssal Hook", "type": "melee", "max_upgrade_level": 10,
            "max_upgrade_damage": 303.0,
            "stats": [{"label": "Base damage", "display_value": "101", "suffix": ""}],
        })
        weapon_fields = {field["name"]: field["value"] for field in weapon.fields}
        self.assertIn("+10", weapon_fields["Upgrade progression"])
        self.assertIn("303", weapon_fields["Upgrade progression"])

        archetype = record_embed("r2", "archetypes", {
            "name": "Alchemist", "flavor_tags": ["Utility", "Support"],
            "trait_name": "Potency",
            "skills": [{"name": "Vial: Elixir Of Life", "unlock_level": 10}],
            "perks": [{"name": "Experimentalist", "perk_label": "Relic Perk"}],
        })
        archetype_fields = {field["name"]: field["value"] for field in archetype.fields}
        self.assertIn("Potency", archetype_fields["Archetype kit"])
        self.assertIn("unlock level 10", archetype_fields["Skills"])
        self.assertIn("Relic Perk", archetype_fields["Perks"])

        boss = record_embed("r2", "bosses", {
            "name": "Abomination", "world": "N'Erud",
            "base_defenses": {"HealthMax": 4000.0, "ShockResistance": -15.0, "AcidResistance": 50.0},
            "rewards": ["Mutated Growth"],
        })
        boss_fields = {field["name"]: field["value"] for field in boss.fields}
        self.assertIn("Shock - -15% weakness", boss_fields["Defenses"])
        self.assertIn("Mutated Growth", boss_fields["Rewards"])

    def test_nr_rune_gem_and_armor_embeds_show_compatibility_and_effects(self):
        rune = record_embed("nr", "runes", {
            "id": "1", "name": "Advancing Flurry",
            "compatible_weapon_class_names": ["Axe", "Spear"],
        })
        self.assertIn("Spear", {field["name"]: field["value"] for field in rune.fields}["Compatible weapons"])

        gem = record_embed("nr", "gems", {
            "id": "2", "name": "Chipped Amethyst",
            "effects": [{"slot": "Weapon", "description": "Gain Plague Infusion"}],
        })
        self.assertIn("Gain Plague Infusion", {field["name"]: field["value"] for field in gem.fields}["Socket effects"])

        armor = record_embed("nr", "armor", {
            "id": "3", "name": "Apprentice's Coat", "armor_class": "Cloth",
            "weight_class": "Cloth", "default_rarity": "common",
            "rarity_options": ["common", "rare", "plagued"],
        })
        self.assertIn("Plagued", {field["name"]: field["value"] for field in armor.fields}["Armor details"])

    def test_ens_challenge_profile_parser_extracts_public_settings(self):
        source = '''
        <h2>ShroudQuest challenge profile</h2>
        <article><h3><i></i>Player survival</h3>
        <p class="uppercase">Player health</p><p>0.75×</p>
        <p class="uppercase">Item durability</p><p>Enabled</p></article>
        </section><section><h2>Another section</h2></section>
        '''
        self.assertEqual(_ens_challenge_groups(source), [
            ("Player survival", [("Player health", "0.75×"), ("Item durability", "Enabled")]),
        ])

    def test_ffxiv_node_status_calculates_active_and_next_change(self):
        # At Unix 0 the Eorzea clock is 00:00.
        active = _ffxiv_node_status({"times": [0], "uptime": 120}, now=0)
        upcoming = _ffxiv_node_status({"times": [2], "uptime": 120}, now=0)
        self.assertTrue(active["active"])
        self.assertEqual(active["remaining_et"], 120)
        self.assertFalse(upcoming["active"])
        self.assertEqual(upcoming["wait_et"], 120)

    def test_housing_preview_balances_districts_and_sizes(self):
        districts = []
        for index, name in enumerate(("Mist", "The Lavender Beds", "The Goblet", "Shirogane", "Empyreum"), start=1):
            districts.append({
                "name": name,
                "plots": [
                    {"ward": index, "plot": 1, "size": "S", "price": 3_000_000},
                    {"ward": index, "plot": 2, "size": "M", "price": 17_000_000},
                    {"ward": index, "plot": 3, "size": "L", "price": 40_000_000},
                ],
            })
        featured = _featured_housing_plots(districts)
        self.assertEqual(len(featured), 8)
        self.assertEqual({plot["district_name"] for plot in featured}, {district["name"] for district in districts})
        self.assertEqual({plot["size"] for plot in featured}, {"S", "M", "L"})


class GameHubFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_ms2_items_include_permanent_pickups_from_reference_payload(self):
        cog = GameHubsCog(SimpleNamespace())
        cog._request = AsyncMock(side_effect=[
            (200, {"datasets": {"ms2_reference": {"url": "https://example.test/ms2.json"}}}),
            (200, {
                "items": [],
                "permanent_pickups": {"items": [{"name": "Bloodseed", "stages": []}]},
            }),
        ])
        rows = await cog._ms2("items")
        self.assertEqual([row["name"] for row in rows], ["Bloodseed"])

    async def test_ms2_exact_weapon_lookup_enriches_compatible_upgrades(self):
        cog = object.__new__(GameHubsCog)
        cog._cache = {}
        cog._catalog = AsyncMock(return_value=[{"name": "Axatana", "source_id": "axatana"}])
        cog._request_text = AsyncMock(return_value=(200, '''
            <p>Works with this weapon</p>
            <article><h3>Grudge Stone</h3><p>Guaranteed Critical Hit</p></article>
            </section>
        '''))
        ctx = SimpleNamespace(reply=AsyncMock())
        await cog._run(ctx, "ms2", "weapons Axatana")
        fields = {field["name"]: field["value"] for field in ctx.reply.await_args.kwargs["embed"].fields}
        self.assertIn("Grudge Stone", fields["Works with this weapon"])

    async def test_search_tolerates_omitted_possessive_apostrophe_and_s(self):
        cog = GameHubsCog(SimpleNamespace())
        matches = cog._rank([{"name": "Acolyte's Stone"}], "Acolyte Stone")
        self.assertEqual(matches[0]["name"], "Acolyte's Stone")

    async def test_r2_skills_are_flattened_from_archetypes(self):
        cog = GameHubsCog(SimpleNamespace())
        cog._r2 = AsyncMock(return_value=[{
            "name": "Handler", "skills": [{"id": 1, "name": "Guard Dog"}],
        }, {
            "name": "Second", "skills": [{"id": 1, "name": "Guard Dog"}, {"id": 2, "name": "Support Dog"}],
        }])
        # Exercise the production flattening with only the recursive archetype
        # call mocked, while avoiding the instance method replacement above.
        original = GameHubsCog._r2
        cog._r2 = original.__get__(cog, GameHubsCog)
        cog._cache = {}
        cog._request = AsyncMock(return_value=(200, {"archetypes": [{
            "name": "Handler", "skills": [{"id": 1, "name": "Guard Dog"}, {"id": 2, "name": "Support Dog"}],
        }]}))
        rows = await cog._r2("skills")
        self.assertEqual([row["name"] for row in rows], ["Guard Dog", "Support Dog"])

    async def test_universal_search_uses_only_selected_game_catalogs(self):
        cog = object.__new__(GameHubsCog)
        cog._catalog = AsyncMock(side_effect=lambda game, category, query="": (
            [{"name": "Flamelost Greatblades"}] if game == "cd" and category == "weapons" else []
        ))
        ctx = SimpleNamespace(reply=AsyncMock())
        await cog._run(ctx, "nr", "Flamelost Greatblades")
        embed = ctx.reply.await_args.kwargs["embed"]
        self.assertIn("No exact No Rest for the Wicked match", embed.description)

    async def test_ff_and_ens_commands_return_questlog_hubs(self):
        cog = GameHubsCog(SimpleNamespace(_cogs={}))
        ff_ctx = SimpleNamespace(reply=AsyncMock())
        ens_ctx = SimpleNamespace(reply=AsyncMock(), guild=None)
        await cog.ff(ff_ctx, topic="housing")
        await cog.ens(ens_ctx, topic="status")
        self.assertIn("ffxiv/tools/housing", ff_ctx.reply.await_args.kwargs["embed"].description)
        self.assertIn("survival/enshrouded", ens_ctx.reply.await_args.kwargs["embed"].description)

    async def test_ens_commands_have_distinct_outputs_and_link_status(self):
        cfg = {
            "instance_name": "ShroudQuest01", "game_type": "Enshrouded",
            "server_display_name": "ShroudQuest", "fluxer_guild_id": "123",
        }
        server_cog = SimpleNamespace(_get_cfg_for_guild=lambda _guild: [cfg])
        cog = GameHubsCog(SimpleNamespace(_cogs={"GameServerCog": server_cog}))
        ctx = SimpleNamespace(reply=AsyncMock(), guild=SimpleNamespace(id="123"))

        await cog.ens(ctx, topic="server")
        server_embed = ctx.reply.await_args.kwargs["embed"]
        self.assertIn("Enshrouded Server", server_embed.title)

        live_embed = fluxer.Embed(title="🔥 ShroudQuest Server Info")
        with patch("cogs.gameserver.build_serverinfo_embed", AsyncMock(return_value=live_embed)):
            await cog.ens(ctx, topic="status")
        status_embed = ctx.reply.await_args.kwargs["embed"]
        self.assertIn("survival/enshrouded", status_embed.url)

        with patch("cogs.gameserver._get_server_status", AsyncMock(return_value={
            "is_running": True, "uptime": "2 days",
        })):
            await cog.ens(ctx, topic="uptime")
        uptime_embed = ctx.reply.await_args.kwargs["embed"]
        self.assertIn("2 days", uptime_embed.description)
        self.assertNotEqual(server_embed.title, uptime_embed.title)

    async def test_ens_settings_and_empty_whois_are_player_friendly(self):
        cfg = {"instance_name": "ShroudQuest01", "game_type": "Enshrouded"}
        server_cog = SimpleNamespace(_get_cfg_for_guild=lambda _guild: [cfg])
        cog = GameHubsCog(SimpleNamespace(_cogs={"GameServerCog": server_cog}))
        cog._request_text = AsyncMock(return_value=(200, '''
            <h2>ShroudQuest challenge profile</h2>
            <article><h3>Enemies and bosses</h3>
            <p class="uppercase">Enemy damage</p><p>1.5×</p></article></section>
        '''))
        ctx = SimpleNamespace(reply=AsyncMock(), guild=SimpleNamespace(id="123"))

        await cog.ens(ctx, topic="settings")
        settings_embed = ctx.reply.await_args.kwargs["embed"]
        fields = {field["name"]: field["value"] for field in settings_embed.fields}
        self.assertIn("Enemy damage", fields["Enemies and bosses"])

        await cog.ens(ctx, topic="whois")
        whois_embed = ctx.reply.await_args.kwargs["embed"]
        self.assertEqual(whois_embed.description, "Usage: `!ens whois <player>`")

    async def test_ens_wiki_search_supports_category_queries(self):
        cog = GameHubsCog(SimpleNamespace(_cogs={}))
        cog._request = AsyncMock(return_value=(200, {"results": [{
            "name": "Example Bow", "source_name": "Weapon_Bow_Example",
            "category": "weapons", "level": 12, "rarity": "Rare",
            "detail_url": "https://questlog.casual-heroes.com/survival/enshrouded/wiki/weapons/example/",
        }]}))
        ctx = SimpleNamespace(reply=AsyncMock(), guild=None)

        await cog.ens(ctx, topic="weapon Example Bow")

        cog._request.assert_awaited_once_with(
            "api/survival/enshrouded/search/",
            params={"q": "Example Bow", "category": "weapons"},
        )
        embed = ctx.reply.await_args.kwargs["embed"]
        self.assertIn("Example Bow", embed.title)
        self.assertIn("Level:** 12", embed.description)

    async def test_ens_crafting_npc_and_quest_aliases_use_canonical_categories(self):
        self.assertEqual(ENS_ALIASES["boss"], "bosses")
        self.assertEqual(ENS_ALIASES["location"], "locations")
        self.assertTrue({"crafting", "npcs", "quests"}.issubset(ENS_CATEGORIES))
        for command, category in (
            ("craft Skull Trophy V", "crafting"),
            ("npc Valory", "npcs"),
            ("quest Finding the Sleeping Survivor", "quests"),
        ):
            with self.subTest(category=category):
                cog = GameHubsCog(SimpleNamespace(_cogs={}))
                cog._request = AsyncMock(return_value=(200, {"results": []}))
                ctx = SimpleNamespace(reply=AsyncMock(), guild=None)
                await cog.ens(ctx, topic=command)
                query = command.split(" ", 1)[1]
                cog._request.assert_awaited_once_with(
                    "api/survival/enshrouded/search/",
                    params={"q": query, "category": category},
                )

    async def test_ens_unknown_topic_becomes_universal_wiki_search(self):
        cog = GameHubsCog(SimpleNamespace(_cogs={}))
        cog._request = AsyncMock(return_value=(200, {"results": []}))
        ctx = SimpleNamespace(reply=AsyncMock(), guild=None)

        await cog.ens(ctx, topic="rising fighter trousers")

        cog._request.assert_awaited_once_with(
            "api/survival/enshrouded/search/",
            params={"q": "rising fighter trousers"},
        )
        self.assertIn("No Enshrouded match", ctx.reply.await_args.kwargs["embed"].title)

    async def test_ff_gathering_and_ocean_return_live_data(self):
        cog = GameHubsCog(SimpleNamespace())
        ctx = SimpleNamespace(reply=AsyncMock(), author=SimpleNamespace(id="7"))
        cog._request = AsyncMock(side_effect=[
            (200, [{
                "name": "Darksteel Ore", "zone": "Coerthas Central Highlands",
                "items": ["Darksteel Ore"], "job": "MIN", "times": [1], "uptime": 120,
            }]),
            (200, {"windows": [{
                "route": "Indigo Route", "departure_ts": 2_000_000_000,
                "blue_fish": ["Sothis"], "is_active": False,
            }]}),
        ])
        await cog.ff(ctx, topic="gathering darksteel")
        gathering = ctx.reply.await_args.kwargs["embed"]
        self.assertIn("Darksteel Ore", gathering.fields[0]["value"])

        await cog.ff(ctx, topic="ocean")
        ocean = ctx.reply.await_args.kwargs["embed"]
        self.assertIn("Indigo Route", ocean.fields[0]["value"])
        self.assertIn("Sothis", ocean.fields[0]["value"])

    async def test_ff_collection_uses_requesting_users_linked_profile(self):
        cog = GameHubsCog(SimpleNamespace())
        ctx = SimpleNamespace(reply=AsyncMock(), author=SimpleNamespace(id="77"))
        profile = {
            "account": "hero",
            "character": {
                "name": "Test Hero", "world": "Halicarnassus", "datacenter": "Dynamis",
                "avatar": "", "active_job": "Paladin", "mount_count": 100,
                "minion_count": 200, "mounts": ["Company Chocobo"], "minions": [],
                "achievement_count": 50, "achievement_total": 100,
                "triad_count": 25, "clear_count": 3, "last_synced_at": 1_700_000_000,
            },
            "deep_dungeon": [], "field_ops": [],
        }
        with patch("cogs.game_hubs._ffxiv_profile_for_fluxer", return_value=profile) as lookup:
            await cog.ff(ctx, topic="mounts")
        lookup.assert_called_once_with("77")
        embed = ctx.reply.await_args.kwargs["embed"]
        text = str(embed.to_dict())
        self.assertIn("Test Hero", text)
        self.assertIn("Company Chocobo", text)
        self.assertIn("100", text)

    async def test_ff_collection_prompts_unlinked_user_to_connect(self):
        cog = GameHubsCog(SimpleNamespace())
        ctx = SimpleNamespace(reply=AsyncMock(), author=SimpleNamespace(id="88"))
        with patch("cogs.game_hubs._ffxiv_profile_for_fluxer", return_value=None):
            await cog.ff(ctx, topic="collection")
        embed = ctx.reply.await_args.kwargs["embed"]
        self.assertIn("not linked", embed.description)

    async def test_ff_housing_returns_live_plot_summary(self):
        cog = GameHubsCog(SimpleNamespace())
        ctx = SimpleNamespace(reply=AsyncMock(), author=SimpleNamespace(id="7"))
        cog._request = AsyncMock(side_effect=[
            (200, {"datacenters": [{"worlds": [{"id": 406, "name": "Halicarnassus"}]}]}),
            (200, {"world_name": "Halicarnassus", "total_open": 2, "districts": [{
                "name": "Mist", "open_count": 2, "plots": [{
                    "ward": 1, "plot": 5, "size": "M", "price": 17_000_000,
                }],
            }]}),
        ])
        await cog.ff(ctx, topic="housing Halicarnassus")
        embed = ctx.reply.await_args.kwargs["embed"]
        self.assertIn("2 open plots", embed.description)
        self.assertIn("Ward 1, Plot 5", str(embed.to_dict()))

    async def test_ff_market_board_returns_prices_for_explicit_world(self):
        cog = GameHubsCog(SimpleNamespace())
        ctx = SimpleNamespace(reply=AsyncMock(), author=SimpleNamespace(id="7"))
        cog._request = AsyncMock(side_effect=[
            (200, {"results": [{"id": 100, "name": "Darksteel Ore", "icon": ""}]}),
            (200, {
                "min_price_nq": 500, "min_price_hq": 750, "units_for_sale": 12,
                "sale_velocity_nq": 2.5, "sale_velocity_hq": 1.0,
                "listings": [{"price": 500, "quantity": 3, "hq": False, "retainer": "Seller"}],
            }),
        ])
        with patch("cogs.game_hubs._ffxiv_profile_for_fluxer", return_value=None):
            await cog.ff(ctx, topic="mb Darksteel Ore | Halicarnassus")
        embed = ctx.reply.await_args.kwargs["embed"]
        self.assertIn("500 gil", embed.description)
        self.assertIn("Seller", str(embed.to_dict()))

    async def test_ff_resets_returns_daily_and_weekly_timers(self):
        cog = GameHubsCog(SimpleNamespace())
        ctx = SimpleNamespace(reply=AsyncMock(), author=SimpleNamespace(id="7"))
        await cog.ff(ctx, topic="resets")
        embed = ctx.reply.await_args.kwargs["embed"]
        self.assertIn("Daily reset", embed.description)
        self.assertIn("Weekly reset", embed.description)
        self.assertIn("<t:", embed.description)


if __name__ == "__main__":
    unittest.main()

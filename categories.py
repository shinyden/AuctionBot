# ─────────────────────────────────────────────────────────────────────────────
# CATEGORIES
# ─────────────────────────────────────────────────────────────────────────────
# To add a new category:
#   1. Add a new key under CATEGORIES
#   2. Set "name" (display name), "aliases" (alternate names users can type),
#      and "pokemon" (exact Pokémon names as they appear in the database)
#
# Aliases and category keys are all case-insensitive when matched.
# ─────────────────────────────────────────────────────────────────────────────

CATEGORIES: dict = {
    "eevee": {
        "name": "Eeveelutions",
        "aliases": [
            "ibui", "eievui", "eeveelutions", "evoli", "eevos",
            "イーブイ", "Ībui", "Évoli",
        ],
        "pokemon": [
            "Eevee", "Partner Eevee", "Vaporeon", "Jolteon", "Flareon",
            "Espeon", "Umbreon", "Leafeon", "Glaceon", "Sylveon",
        ],
    },

    "genderdifference": {
        "name": "Gender Difference Pokémons",
        "aliases": ["gd", "gender", "genderdiff", "gender_difference"],
        "pokemon": [
            "Hisuian Sneasel", "Basculegion", "Butterfree", "Kricketune",
            "Hippopotas", "Oinkologne", "Vileplume", "Sudowoodo", "Wobbuffet",
            "Girafarig", "Heracross", "Piloswine", "Octillery", "Combusken",
            "Beautifly", "Relicanth", "Staraptor", "Kricketot", "Pachirisu",
            "Hippowdon", "Toxicroak", "Abomasnow", "Rhyperior", "Tangrowth",
            "Mamoswine", "Jellicent", "Venusaur", "Raticate", "Alakazam",
            "Magikarp", "Gyarados", "Meganium", "Politoed", "Quagsire",
            "Ursaring", "Houndoom", "Blaziken", "Ludicolo", "Meditite",
            "Medicham", "Camerupt", "Cacturne", "Staravia", "Roserade",
            "Floatzel", "Garchomp", "Croagunk", "Lumineon", "Unfezant",
            "Frillish", "Meowstic", "Indeedee", "Rattata", "Pikachu",
            "Kadabra", "Rhyhorn", "Goldeen", "Seaking", "Scyther",
            "Murkrow", "Steelix", "Sneasel", "Donphan", "Torchic",
            "Nuzleaf", "Shiftry", "Roselia", "Milotic", "Bibarel",
            "Ambipom", "Finneon", "Weavile", "Raichu", "Golbat",
            "Dodrio", "Rhydon", "Ledyba", "Ledian", "Wooper",
            "Gligar", "Scizor", "Dustox", "Gulpin", "Swalot",
            "Starly", "Bidoof", "Luxray", "Combee", "Buizel",
            "Gabite", "Snover", "Pyroar", "Zubat", "Gloom",
            "Doduo", "Hypno", "Eevee", "Aipom", "Numel",
            "Shinx", "Luxio", "Gible", "Xatu",
        ],
    },

    "starters": {
        "name": "Starter Pokémon",
        "aliases": ["starter", "start", "starters"],
        "pokemon": [
            "Bulbasaur", "Ivysaur", "Venusaur",
            "Charmander", "Charmeleon", "Charizard",
            "Squirtle", "Wartortle", "Blastoise",
            "Chikorita", "Bayleef", "Meganium",
            "Cyndaquil", "Quilava", "Typhlosion",
            "Totodile", "Croconaw", "Feraligatr",
            "Treecko", "Grovyle", "Sceptile",
            "Torchic", "Combusken", "Blaziken",
            "Mudkip", "Marshtomp", "Swampert",
            "Turtwig", "Grotle", "Torterra",
            "Chimchar", "Monferno", "Infernape",
            "Piplup", "Prinplup", "Empoleon",
            "Snivy", "Servine", "Serperior",
            "Tepig", "Pignite", "Emboar",
            "Oshawott", "Dewott", "Samurott",
            "Chespin", "Quilladin", "Chesnaught",
            "Fennekin", "Braixen", "Delphox",
            "Froakie", "Frogadier", "Greninja",
            "Rowlet", "Dartrix", "Decidueye",
            "Litten", "Torracat", "Incineroar",
            "Popplio", "Brionne", "Primarina",
            "Grookey", "Thwackey", "Rillaboom",
            "Scorbunny", "Raboot", "Cinderace",
            "Sobble", "Drizzile", "Inteleon",
            "Sprigatito", "Floragato", "Meowscarada",
            "Fuecoco", "Crocalor", "Skeledirge",
            "Quaxly", "Quaxwell", "Quaquaval",
        ],
    },

    "legendaries": {
        "name": "Legendary Pokémon",
        "aliases": ["legendary", "legend", "legends", "leg"],
        "pokemon": [
            "Gigantamax Single Strike Urshifu",
            "Gigantamax Rapid Strike Urshifu", "Sprinting Build Koraidon",
            "Hearthflame Mask Ogerpon", "Cornerstone Mask Ogerpon",
            "Wellspring Mask Ogerpon", "Gliding Build Koraidon",
            "Rapid Strike Urshifu", "Shadow Rider Calyrex",
            "Dawn Wings Necrozma", "Eternamax Eternatus",
            "Drive Mode Miraidon", "Glide Mode Miraidon", "Dusk Mane Necrozma",
            "Terastal Terapagos", "Galarian Articuno", "Therian Thundurus",
            "Electric Silvally", "Fighting Silvally", "Crowned Zamazenta",
            "Ice Rider Calyrex", "Galarian Moltres", "Therian Tornadus",
            "Therian Landorus", "Complete Zygarde", "Psychic Silvally",
            "Therian Enamorus", "Galarian Zapdos", "Origin Giratina",
            "Neutral Xerneas", "Dragon Silvally", "Flying Silvally",
            "Ground Silvally", "Poison Silvally", "Primal Groudon",
            "Ghost Silvally", "Grass Silvally", "Steel Silvally",
            "Water Silvally", "Fairy Silvally", "Ultra Necrozma",
            "Crowned Zacian", "Mega Mewtwo X", "Mega Mewtwo Y",
            "Primal Kyogre", "Mega Rayquaza", "Origin Dialga", "Origin Palkia",
            "Dark Silvally", "Fire Silvally", "Rock Silvally", "Black Kyurem",
            "White Kyurem", "Zygarde Cell", "Zygarde Core", "Bug Silvally",
            "Ice Silvally", "Mega Latias", "Mega Latios", "10% Zygarde",
            "Fezandipiti", "Type: Null", "Registeel", "Regigigas", "Cresselia",
            "Terrakion", "Thundurus", "Tapu Koko", "Tapu Lele", "Tapu Bulu",
            "Tapu Fini", "Zamazenta", "Eternatus", "Regieleki", "Regidrago",
            "Glastrier", "Spectrier", "Chien-Pao", "Munkidori", "Terapagos",
            "Articuno", "Regirock", "Rayquaza", "Giratina", "Cobalion",
            "Virizion", "Tornadus", "Reshiram", "Landorus", "Silvally",
            "Solgaleo", "Necrozma", "Enamorus", "Wo-Chien", "Koraidon",
            "Miraidon", "Moltres", "Suicune", "Groudon", "Mesprit", "Heatran",
            "Xerneas", "Yveltal", "Zygarde", "Cosmoem", "Urshifu", "Calyrex",
            "Ting-Lu", "Okidogi", "Ogerpon", "Zapdos", "Mewtwo", "Raikou",
            "Regice", "Latias", "Latios", "Kyogre", "Dialga", "Palkia",
            "Zekrom", "Kyurem", "Cosmog", "Lunala", "Zacian", "Chi-Yu",
            "Entei", "Lugia", "Ho-Oh", "Azelf", "Kubfu", "Uxie",
        ],
    },

    "mythicals": {
        "name": "Mythical Pokémon",
        "aliases": ["mythical", "myth", "myths"],
        "pokemon": [
            "High-speed Flight Configuration Genesect", "Gigantamax Melmetal",
            "Pirouette Meloetta", "Original Magearna", "Zenith Marshadow",
            "Electric Arceus", "Fighting Arceus", "Resolute Keldeo",
            "Defense Deoxys", "Psychic Arceus", "Attack Deoxys",
            "Dragon Arceus", "Flying Arceus", "Ground Arceus", "Poison Arceus",
            "Hoopa Unbound", "Speed Deoxys", "Ghost Arceus", "Grass Arceus",
            "Steel Arceus", "Water Arceus", "Fairy Arceus", "Mega Diancie",
            "Sky Shaymin", "Dark Arceus", "Fire Arceus", "Rock Arceus",
            "Dada Zarude", "Bug Arceus", "Ice Arceus", "Volcanion",
            "Marshadow", "Pecharunt", "Meloetta", "Genesect", "Magearna",
            "Melmetal", "Jirachi", "Manaphy", "Darkrai", "Shaymin", "Victini",
            "Diancie", "Zeraora", "Celebi", "Deoxys", "Phione", "Arceus",
            "Keldeo", "Meltan", "Zarude", "Hoopa", "Mew",
        ],
    },

    "ultrabeast": {
        "name": "UltraBeast Pokémon",
        "aliases": ["ub", "ultrab", "ubs"],
        "pokemon": [
            "Blacephalon", "Celesteela", "Pheromosa", "Xurkitree", "Naganadel",
            "Stakataka", "Nihilego", "Buzzwole", "Guzzlord", "Kartana",
            "Poipole",
        ],
    },

    "gmax": {
        "name": "Gigantamax Pokémon",
        "aliases": ["gigantamax", "gigantmax", "g-max"],
        "pokemon": [
            "Gigantamax Venusaur", "Gigantamax Charizard", "Gigantamax Blastoise",
            "Gigantamax Butterfree", "Gigantamax Pikachu", "Gigantamax Meowth",
            "Gigantamax Machamp", "Gigantamax Gengar", "Gigantamax Kingler",
            "Gigantamax Lapras", "Gigantamax Eevee", "Gigantamax Snorlax",
            "Gigantamax Garbodor", "Gigantamax Melmetal", "Gigantamax Rillaboom",
            "Gigantamax Cinderace", "Gigantamax Inteleon", "Gigantamax Corviknight",
            "Gigantamax Orbeetle", "Gigantamax Drednaw", "Gigantamax Coalossal",
            "Gigantamax Flapple", "Gigantamax Appletun", "Gigantamax Sandaconda",
            "Gigantamax Toxtricity", "Gigantamax Centiskorch", "Gigantamax Hatterene",
            "Gigantamax Grimmsnarl", "Gigantamax Alcremie", "Gigantamax Copperajah",
            "Gigantamax Duraludon", "Gigantamax Urshifu",
        ],
    },

    "regionals": {
        "name":
        "Regional Pokémon",
        "aliases": ["regional", "reg", "regionalpokemons"],
        "pokemon": [
            "Galarian Zen Darmanitan", "Galarian Farfetch'd",
            "Combat Breed Tauros", "Galarian Darmanitan", "Blaze Breed Tauros",
            "Hisuian Typhlosion", "Galarian Zigzagoon", "Hisuian Growlithe",
            "Galarian Rapidash", "Galarian Slowpoke", "Hisuian Electrode",
            "Galarian Mr. Mime", "Aqua Breed Tauros", "Galarian Articuno",
            "Galarian Slowking", "Hisuian Lilligant", "Galarian Darumaka",
            "Galarian Stunfisk", "Hisuian Decidueye", "Alolan Sandshrew",
            "Alolan Sandslash", "Alolan Ninetales", "Hisuian Arcanine",
            "Galarian Slowbro", "Alolan Exeggutor", "Galarian Weezing",
            "Galarian Moltres", "Hisuian Qwilfish", "Galarian Corsola",
            "Galarian Linoone", "Hisuian Samurott", "Hisuian Braviary",
            "Alolan Raticate", "Galarian Meowth", "Alolan Graveler",
            "Galarian Ponyta", "Hisuian Voltorb", "Galarian Zapdos",
            "Hisuian Sneasel", "Galarian Yamask", "Hisuian Zoroark",
            "Hisuian Sliggoo", "Hisuian Avalugg", "Alolan Rattata",
            "Alolan Diglett", "Alolan Dugtrio", "Alolan Persian",
            "Alolan Geodude", "Alolan Marowak", "Paldean Wooper",
            "Hisuian Goodra", "Alolan Raichu", "Alolan Vulpix",
            "Alolan Meowth", "Alolan Grimer", "Hisuian Zorua", "Alolan Golem",
            "Alolan Muk",
        ]

    },

    "rares": {
        "name": "Rare Pokémon",
        "aliases": ["rare", "r"],
        "pokemon": [
"Registeel","Articuno","Regirock","Rayquaza","Moltres","Suicune","Groudon","Jirachi","Zapdos","Mewtwo","Raikou","Celebi","Regice","Latias","Latios","Kyogre","Entei","Lugia","Ho-Oh","Mew","Regigigas","Cresselia","Terrakion","Giratina","Cobalion","Virizion","Tornadus","Mesprit","Heatran","Manaphy","Darkrai","Shaymin","Victini","Deoxys","Dialga","Palkia","Phione","Arceus","Azelf","Uxie","Type: Null","Thundurus","Volcanion","Tapu Koko","Tapu Lele","Tapu Bulu","Tapu Fini","Reshiram","Landorus","Meloetta","Genesect","Silvally","Xerneas","Yveltal","Zygarde","Diancie","Zekrom","Kyurem","Keldeo","Hoopa","Blacephalon","Celesteela","Pheromosa","Xurkitree","Marshadow","Naganadel","Stakataka","Solgaleo","Nihilego","Buzzwole","Guzzlord","Necrozma","Magearna","Cosmoem","Kartana","Poipole","Zeraora","Cosmog","Lunala","Meltan","Zamazenta","Eternatus","Regieleki","Regidrago","Glastrier","Spectrier","Chien-Pao","Melmetal","Enamorus","Wo-Chien","Koraidon","Miraidon","Urshifu","Calyrex","Ting-Lu","Okidogi","Zacian","Zarude","Chi-Yu","Kubfu","Pirouette Meloetta","Therian Thundurus","Therian Tornadus","Therian Landorus","Origin Giratina","Resolute Keldeo","Defense Deoxys","Mega Mewtwo X","Mega Mewtwo Y","Attack Deoxys","Speed Deoxys","Black Kyurem","White Kyurem","Mega Latias","Sky Shaymin","Fezandipiti","Munkidori","Terapagos","Pecharunt","Ogerpon","Rapid Strike Urshifu","Shadow Rider Calyrex" ,"Dawn Wings Necrozma","Dusk Mane Necrozma","Galarian Articuno","Original Magearna","Crowned Zamazenta","Ice Rider Calyrex","Galarian Moltres","Complete Zygarde","Galarian Zapdos","Primal Groudon","Ultra Necrozma","Crowned Zacian","Primal Kyogre","Mega Rayquaza","Hoopa Unbound","Mega Diancie","Mega Latios","10% Zygarde","Gigantamax Single Strike Urshifu","Gigantamax Rapid Strike Urshifu","Sprinting Build Koraidon","Hearthflame Mask Ogerpon","Cornerstone Mask Ogerpon","Wellspring Mask Ogerpon","Gliding Build Koraidon","Gigantamax Melmetal","Eternamax Eternatus","Drive Mode Miraidon","Glide Mode Miraidon","Terastal Terapagos","Therian Enamorus","Neutral Xerneas","Origin Dialga","Origin Palkia","Dragon Arceus","Dark Arceus","Dada Zarude","Bug Arceus","Electric Silvally","Fighting Silvally","Electric Arceus","Fighting Arceus","Dragon Silvally","Psychic Arceus","Flying Arceus","Ground Arceus","Poison Arceus","Dark Silvally","Fire Silvally","Ghost Arceus","Grass Arceus","Steel Arceus","Water Arceus","Fairy Arceus","Bug Silvally","Fire Arceus","Rock Arceus","Ice Arceus","High-speed Flight Configuration Genesect","Psychic Silvally","Zenith Marshadow","Flying Silvally","Ground Silvally","Poison Silvally","Ghost Silvally","Grass Silvally","Steel Silvally","Water Silvally","Fairy Silvally","Rock Silvally","Zygarde Cell","Zygarde Core","Ice Silvally",
        ],
    },

    # ── ADD NEW CATEGORIES BELOW ──────────────────────────────────────────────
    # "my_category": {
    #     "name": "My Category Display Name",
    #     "aliases": ["alias1", "alias2"],
    #     "pokemon": ["Pokemon1", "Pokemon2"],
    # },
}


# ─── Lookup helpers (built at import time for O(1) access) ───────────────────

def _build_lookup() -> dict[str, str]:
    """Returns {alias_lower -> category_key} including the key itself."""
    lookup: dict[str, str] = {}
    for key, data in CATEGORIES.items():
        lookup[key.lower()] = key
        for alias in data.get("aliases", []):
            lookup[alias.lower()] = key
    return lookup

_ALIAS_LOOKUP: dict[str, str] = _build_lookup()


def resolve_category(name: str) -> dict | None:
    """
    Given a user-supplied category name/alias, return the category dict
    (with keys 'name', 'aliases', 'pokemon') or None if not found.
    """
    key = _ALIAS_LOOKUP.get(name.lower())
    if key is None:
        return None
    return CATEGORIES[key]


def list_categories() -> list[dict]:
    """Return list of all categories with their display names and aliases."""
    return [
        {"key": k, "name": v["name"], "aliases": v["aliases"]}
        for k, v in CATEGORIES.items()
    ]

"""把官方 ConfigToJson（PascalCase / 原始包装）转成手册流水线期望的 JSON 形状。"""

from __future__ import annotations

from typing import Any


def _strip_meta(data: dict) -> dict:
    return {k: v for k, v in data.items() if k != "fileName"}


def _null_to_empty(v: Any) -> Any:
    if v is None:
        return []
    return v


def _items_as_root(data: dict) -> dict:
    body = _strip_meta(data)
    if "Items" in body:
        return {"root": body["Items"] if body["Items"] is not None else []}
    if "items" in body:
        return {"root": body["items"] if body["items"] is not None else []}
    if "data" in body:
        return {"data": body["data"]}
    if "root" in body:
        root = body["root"]
        if isinstance(root, dict) and "item" in root and len(root) == 1:
            return {"root": root["item"] if root["item"] is not None else []}
        return {"root": root}
    if "config" in body and isinstance(body["config"], dict):
        return {"root": body["config"]}
    # 单 key 包一层
    if len(body) == 1:
        only = next(iter(body.values()))
        if isinstance(only, list):
            return {"root": only}
    return body


def adapt_moves(data: dict) -> dict:
    """官方 MovesTbl → moves_unity.json 的 snake_case root.moves.move。"""
    moves_tbl = data.get("MovesTbl") or {}
    moves = (moves_tbl.get("Moves") or {})
    raw_list = moves.get("Move") or []
    text = moves.get("_text") or moves.get("text") or ""
    key_map = {
        "Accuracy": "accuracy",
        "AtkNum": "atk_num",
        "AtkType": "atk_type",
        "Category": "category",
        "CritRate": "crit_rate",
        "FriendSideEffect": "friend_side_effect",
        "FriendSideEffectArg": "friend_side_effect_arg",
        "ID": "id",
        "MaxPP": "max_pp",
        "MonID": "mon_id",
        "MustHit": "must_hit",
        "Name": "name",
        "Power": "power",
        "Priority": "priority",
        "SideEffect": "side_effect",
        "SideEffectArg": "side_effect_arg",
        "Type": "type",
        "info": "info",
        "ordinary": "ordinary",
    }
    out_moves = []
    for m in raw_list:
        item = {}
        for k, v in m.items():
            nk = key_map.get(k, k)
            if nk in (
                "friend_side_effect",
                "friend_side_effect_arg",
                "side_effect",
                "side_effect_arg",
            ):
                item[nk] = _null_to_empty(v)
            else:
                item[nk] = v
        item.setdefault("friend_side_effect", [])
        item.setdefault("friend_side_effect_arg", [])
        item.setdefault("side_effect", [])
        item.setdefault("side_effect_arg", [])
        out_moves.append(item)
    return {"root": {"moves": {"move": out_moves, "text": text}}}


def adapt_effect_icon(data: dict) -> dict:
    root = (data.get("root") or {})
    effects = root.get("effect") or []
    key_map = {
        "Id": "id",
        "effectId": "effect_id",
        "iconId": "icon_id",
        "isAdv": "is_adv",
        "limitedType": "limited_type",
        "petId": "pet_id",
        "specificId": "specific_id",
    }
    out = []
    for e in effects:
        item = {}
        for k, v in e.items():
            nk = key_map.get(k, k)
            if nk in ("des", "tag", "kind", "pet_id", "specific_id"):
                item[nk] = _null_to_empty(v)
            else:
                item[nk] = v
        out.append(item)
    return {"root": {"effect": out}}


def adapt_effect_info(data: dict) -> dict:
    root = data.get("root") or {}
    effects = root.get("Effect") or root.get("effect") or []
    params = root.get("ParamType") or root.get("param_type") or []
    out_e = []
    for e in effects:
        out_e.append(
            {
                "analyze": e.get("analyze", ""),
                "info": e.get("info", ""),
                "param": _null_to_empty(e.get("param")),
                "args_num": e.get("args_num", e.get("argsNum", 0)),
                "id": e.get("id", 0),
                "key": e.get("key", ""),
                "type": e.get("type", 0),
            }
        )
    out_p = []
    for p in params:
        out_p.append({"id": p.get("id", 0), "params": p.get("params", "")})
    return {"root": {"effect": out_e, "param_type": out_p}}


def adapt_gems(data: dict) -> dict:
    gems = ((data.get("Gems") or {}).get("Gem")) or []
    out = []
    for g in gems:
        skill_effects = []
        for se in g.get("SkillEffects") or []:
            eff = (se.get("Effect") or se.get("effect") or {})
            skill_effects.append(
                {
                    "effect": {
                        "effect_id": eff.get("EffectId", eff.get("effect_id", 0)),
                        "param": _null_to_empty(eff.get("Param", eff.get("param"))),
                    }
                }
            )
        out.append(
            {
                "category": g.get("Category", g.get("category", 0)),
                "decompose_prob": g.get("DecomposeProb", g.get("decompose_prob", 0)),
                "des": g.get("Des", g.get("des", "")),
                "equit_lv1_cnt1": g.get("EquitLv1Cnt1", g.get("equit_lv1_cnt1", 0)),
                "gid": g.get("ID", g.get("gid", 0)),
                "lv": g.get("Lv", g.get("lv", 0)),
                "name": g.get("Name", g.get("name", "")),
                "skill_effects": skill_effects,
                "upgrade_gem_id": g.get("UpgradeGemId", g.get("upgrade_gem_id", 0)),
            }
        )
    return {"gems": {"gem": out}}


def adapt_move_stones(data: dict) -> dict:
    stones = ((data.get("MoveStones") or {}).get("MoveStone")) or []
    out = []
    for s in stones:
        effects = []
        for me in s.get("MoveEffect") or s.get("move_effect") or []:
            effects.append(
                {
                    "id": me.get("ID", me.get("id", 0)),
                    "side_effect": _null_to_empty(
                        me.get("SideEffect", me.get("side_effect"))
                    ),
                    "side_effect_arg": _null_to_empty(
                        me.get("SideEffectArg", me.get("side_effect_arg"))
                    ),
                }
            )
        out.append(
            {
                "accuracy": s.get("Accuracy", s.get("accuracy", 0)),
                "id": s.get("ID", s.get("id", 0)),
                "max_pp": s.get("MaxPP", s.get("max_pp", 0)),
                "move_effect": effects,
                "name": s.get("Name", s.get("name", "")),
                "power": s.get("Power", s.get("power", 0)),
                "type": s.get("Type", s.get("type", 0)),
            }
        )
    return {"root": out}


def adapt_mintmark(data: dict) -> dict:
    mm = data.get("MintMarks") or {}
    # 手册代码读 MintMarkClass；官方为 MintmarkClass
    classes = mm.get("MintMarkClass")
    if classes is None:
        classes = mm.get("MintmarkClass") or []
    marks = mm.get("MintMark") or []
    # 官方 JSON 常把缺少数组写成 null；.get(k, []) 挡不住 null
    list_keys = (
        "Arg",
        "BaseAttriValue",
        "ExtraAttriValue",
        "MaxAttriValue",
        "MonsterID",
    )
    cleaned = []
    for m in marks:
        if not isinstance(m, dict):
            cleaned.append(m)
            continue
        item = dict(m)
        for k in list_keys:
            if item.get(k) is None:
                item[k] = []
        cleaned.append(item)
    return {
        "MintMarks": {
            "MintMark": cleaned,
            "MintMarkClass": classes,
        }
    }


def adapt_buff(data: dict) -> dict:
    items = data.get("Items") or data.get("data") or []
    out = []
    for i in items:
        icon = i.get("icon")
        if icon is None:
            icon = []
        out.append(
            {
                "Desc": i.get("Desc", ""),
                "Tag": i.get("Tag", ""),
                "desc_tag": i.get("desc_tag", ""),
                "icon": icon,
                "icontype": i.get("icontype", 0),
                "id": i.get("id", 0),
            }
        )
    return {"data": out}


def adapt_effectag(data: dict) -> dict:
    items = data.get("Items") or data.get("data") or []
    return {"data": items if items is not None else []}


def adapt_pet_effect_icon(data: dict) -> dict:
    items = data.get("Items") or data.get("data") or []
    return {"data": items if items is not None else []}


def adapt_skill_effect(data: dict) -> dict:
    items = data.get("Items") or data.get("data") or []
    return {"data": items if items is not None else []}


def adapt_skill_types(data: dict) -> dict:
    root = data.get("root")
    if isinstance(root, dict) and "item" in root:
        return {"root": root["item"] or []}
    if isinstance(root, list):
        return {"root": root}
    return {"root": []}


def adapt_pet_skin(data: dict) -> dict:
    skins = ((data.get("PetSkins") or {}).get("Skin")) or data.get("root") or []
    return {"root": skins if isinstance(skins, list) else []}


def adapt_achievements(data: dict) -> dict:
    rules = data.get("AchievementRules") or {}
    types = rules.get("type") or data.get("root") or []
    return {"root": types if isinstance(types, list) else []}


def adapt_equip(data: dict) -> dict:
    equips = ((data.get("Equips") or {}).get("Equip")) or data.get("root") or []
    return {"root": equips if isinstance(equips, list) else []}


def adapt_sign_icon_fight(data: dict) -> dict:
    cfg = data.get("config") or {}
    items = cfg.get("item") if isinstance(cfg, dict) else None
    if items is None:
        items = data.get("root") or []
    return {"root": items if isinstance(items, list) else []}


def adapt_battle_effects(data: dict) -> dict:
    be = ((data.get("BattleEffects") or {}).get("BattleEffect")) or data.get("root") or []
    return {"root": be if isinstance(be, list) else []}


def adapt_new_se(data: dict) -> dict:
    items = ((data.get("NewSe") or {}).get("NewSeIdx")) or data.get("root") or []
    return {"root": items if isinstance(items, list) else []}


def adapt_new_super_design(data: dict) -> dict:
    root = data.get("Root") or data.get("root") or {}
    if isinstance(root, dict) and "Design" in root:
        return {"root": root["Design"] or []}
    if isinstance(root, list):
        return {"root": root}
    return {"root": []}


def adapt_fragment(data: dict) -> dict:
    root = data.get("Root") or data.get("root") or {}
    if isinstance(root, dict) and "Fragment" in root:
        return {"root": root["Fragment"] or []}
    if isinstance(root, list):
        return {"root": root}
    return {"root": []}


def adapt_items_cat(data: dict) -> dict:
    cats = data.get("cats") or data.get("root") or data.get("Items") or []
    return {"root": cats if isinstance(cats, list) else []}


def adapt_items_list(data: dict) -> dict:
    """itemsOptimizeCatItems* / itemsTip。"""
    if "items" in data:
        return {"root": data["items"] or []}
    return _items_as_root(data)


def adapt_sp_hide_moves(data: dict) -> dict:
    cfg = data.get("config") or data.get("root") or {}
    if not isinstance(cfg, dict):
        return {"root": {"ShowMoves": [], "SpMoves": []}}
    return {
        "root": {
            "ShowMoves": cfg.get("ShowMoves") or [],
            "SpMoves": cfg.get("SpMoves") or [],
        }
    }


def adapt_suit(data: dict) -> dict:
    root = data.get("root")
    if isinstance(root, dict) and "item" in root:
        return {"root": root["item"] or []}
    if isinstance(root, list):
        return {"root": root}
    return {"root": []}


def adapt_passthrough_root(data: dict) -> dict:
    """已有 root 且结构基本可用（pet_advance / awakendetail / effectDes / effectbuff）。"""
    body = _strip_meta(data)
    if "root" in body:
        return {"root": body["root"]}
    return body


def adapt_monsters(data: dict) -> dict:
    body = _strip_meta(data)
    if "Monsters" in body:
        return {"Monsters": body["Monsters"]}
    return body


# name -> adapter；未列出的走通用 Items→root
_ADAPTERS = {
    "moves": adapt_moves,
    "buff": adapt_buff,
    "effectIcon": adapt_effect_icon,
    "effectDes": adapt_passthrough_root,
    "effectbuff": adapt_passthrough_root,
    "effectag": adapt_effectag,
    "effectInfo": adapt_effect_info,
    "skill_effect": adapt_skill_effect,
    "move_stones": adapt_move_stones,
    "monsters": adapt_monsters,
    "awakendetail": adapt_passthrough_root,
    "mintmark": adapt_mintmark,
    "skillTypes": adapt_skill_types,
    "sp_hide_moves": adapt_sp_hide_moves,
    "petEffectIcon": adapt_pet_effect_icon,
    "pet_advance": adapt_passthrough_root,
    "pet_skin": adapt_pet_skin,
    "gems": adapt_gems,
    "itemsOptimizeCat": adapt_items_cat,
    "itemsTip": adapt_items_list,
    "achievements": adapt_achievements,
    "suit": adapt_suit,
    "equip": adapt_equip,
    "signIcon_fight": adapt_sign_icon_fight,
    "battle_effects": adapt_battle_effects,
    "pet_skin_rewardtype": _items_as_root,
    "new_se": adapt_new_se,
    "new_super_design": adapt_new_super_design,
    "archivesBook": _items_as_root,
    "archivesStory": _items_as_root,
    "Fragment": adapt_fragment,
    "partner": _items_as_root,
    "partnerEffectUpgrade": _items_as_root,
    "battlepass_shop": _items_as_root,
    "sp_hide_moves_shop": _items_as_root,
    "exchange_clt": _items_as_root,
    "Activity_TimeUpdateConfig": _items_as_root,
    "pvp_ban": _items_as_root,
    "pvp_ban_expert": _items_as_root,
    "pvp_vote": _items_as_root,
}


def adapt_official_json(name: str, data: dict) -> dict:
    """按配置名把官方 JSON 转成手册期望结构。"""
    if name.startswith("itemsOptimizeCatItems"):
        return adapt_items_list(data)
    fn = _ADAPTERS.get(name)
    if fn is None:
        return _items_as_root(data)
    return fn(data)


def output_filename_for(name: str) -> str:
    """落盘文件名（相对 新数据/）。"""
    if name == "moves":
        return "moves_unity.json"
    return f"{name}.json"

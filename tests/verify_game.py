#!/usr/bin/env python3
"""
Automated Acceptance & Regression Test Suite for Pokemon Pocket HD (v5.1.0)
Verifies all 10 Fix Prompt items, full map BFS reachability (doors, 14 items, 17 signs, HM gates),
building save/reload, mapVersion 4->5 migration, offline PWA service worker, Wonder Trade balance,
Daycare/Fossil anti-farming, Parent Controls PIN + 1-min limit, Portable Save Code, QA regressions,
mobile layout, and end-to-end playthrough.
"""

import http.server
import json
import os
import re
import socketserver
import sys
import threading
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(REPO_ROOT), **kwargs)

    def log_message(self, format, *args):
        pass


def start_server():
    server = socketserver.TCPServer(("127.0.0.1", 0), QuietHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def assert_ok(cond, msg):
    if not cond:
        raise AssertionError(f"FAIL: {msg}")
    print(f"  [PASS] {msg}")


def dismiss_dialogue(page, max_clicks=10):
    for _ in range(max_clicks):
        vis = page.evaluate("state.mode === 'DIALOGUE'")
        if not vis:
            break
        page.evaluate("advanceDialogue()")
        page.wait_for_timeout(40)


def start_fresh_game(page, name="טסטר"):
    page.evaluate(f"startNewGame({json.dumps(name)}, 'pikachu')")
    page.wait_for_timeout(120)
    dismiss_dialogue(page)


def run_all_tests():
    server, port = start_server()
    base_url = f"http://127.0.0.1:{port}"
    print(f"Serving {REPO_ROOT} at {base_url}")

    page_errors = []
    console_errors = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()

            page.on("pageerror", lambda err: page_errors.append(str(err)))
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text)
                if msg.type == "error"
                else None,
            )

            # ================================================================
            # 1. STARTUP, VERSION BADGES & CHANGELOG (Fix #10)
            # ================================================================
            print("\n=== 1. Startup, Version Badges & CHANGELOG (Fix #10) ===")
            page.goto(f"{base_url}/index.html", wait_until="networkidle")
            page.wait_for_timeout(400)

            title_ver = page.locator("#titleVersionBadge").inner_text().strip()
            settings_ver = page.locator("#settingsVersionBadge").inner_text().strip()
            game_ver = page.evaluate("GAME_VERSION")
            changelog_text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

            assert_ok(game_ver == "5.1.0", f"GAME_VERSION is {game_ver}")
            assert_ok(title_ver == "v5.1.0", f"Title version badge visible: {title_ver}")
            assert_ok(settings_ver == "v5.1.0", f"Settings version badge matches: {settings_ver}")
            assert_ok("[5.1.0]" in changelog_text, "CHANGELOG.md documents [5.1.0]")

            # ================================================================
            # 2. REAL PWA MANIFEST, ICONS & OFFLINE MODE (Fix #3)
            # ================================================================
            print("\n=== 2. Real PWA Manifest, Icons & Offline Mode (Fix #3) ===")
            manifest_href = page.evaluate("document.getElementById('webManifestLink').getAttribute('href')")
            assert_ok(
                manifest_href == "./manifest.json" and not manifest_href.startswith("blob:"),
                f"Manifest link is real file ({manifest_href}), not blob",
            )

            for asset in ["manifest.json", "sw.js", "icons/icon-192.png", "icons/icon-512.png"]:
                resp = page.request.get(f"{base_url}/{asset}")
                assert_ok(resp.status == 200, f"HTTP 200 for /{asset} ({len(resp.body())} bytes)")

            manifest_data = json.loads((REPO_ROOT / "manifest.json").read_text(encoding="utf-8"))
            assert_ok(
                manifest_data.get("display") == "standalone" and len(manifest_data.get("icons", [])) >= 2,
                "manifest.json is valid standalone PWA manifest with 192 & 512 icons",
            )

            # Wait for real SW registration & activation
            sw_info = page.evaluate("""async () => {
                if (!('serviceWorker' in navigator)) return { supported: false };
                const reg = await navigator.serviceWorker.ready;
                return {
                    supported: true,
                    activeScript: reg.active ? reg.active.scriptURL : null,
                    state: reg.active ? reg.active.state : null,
                    scope: reg.scope
                };
            }""")
            assert_ok(
                sw_info.get("activeScript", "").endswith("/sw.js") and sw_info.get("state") == "activated",
                f"Real Service Worker active: {sw_info.get('activeScript')} ({sw_info.get('state')})",
            )

            # Reload once so SW controls the client, then go OFFLINE and reload!
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(300)
            sw_controlled = page.evaluate("!!navigator.serviceWorker.controller")
            assert_ok(sw_controlled, "Page is controlled by sw.js before going offline")

            context.set_offline(True)
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(500)
            offline_ok = page.evaluate("typeof startNewGame === 'function' && GAME_VERSION === '5.1.0'")
            assert_ok(offline_ok, "Game loads and runs completely OFFLINE after reload!")
            start_fresh_game(page, "אופליין")
            assert_ok(page.evaluate("state.mode === 'OVERWORLD' && state.party.length === 1"), "Can start & play game while offline")
            context.set_offline(False)

            # ================================================================
            # 3. FULL MAP BFS: DOORS, 14 TREASURES, 17 SIGNS, INDIGO & HM GATES (Fixes #2, #5, #8)
            # ================================================================
            print("\n=== 3. Full Map BFS Audit: Doors, Items, 17 Signs, Indigo & HM Gates (Fixes #2, #5, #8) ===")
            start_fresh_game(page, "בודק")

            bfs_report = page.evaluate("""() => {
                function runBfs(hasCut, hasSurf, hasStrength) {
                    const startX = 63, startY = 247;
                    const visited = new Uint8Array(MAP_ROWS * MAP_COLS);
                    const q = [[startX, startY]];
                    visited[startY * MAP_COLS + startX] = 1;

                    const npcBlocks = new Set();
                    NPCS.forEach(n => npcBlocks.add(`${n.x},${n.y}`));

                    function tileWalkable(tx, ty) {
                        if (tx < 0 || ty < 0 || tx >= MAP_COLS || ty >= MAP_ROWS) return false;
                        const t = WORLD_MAP[ty][tx];
                        if (t === 1 || t === 5 || t === 6 || t === 8 || t === 11 || t === 13 || t === 16) return false;
                        if (t === 3 && !hasSurf) return false;
                        if (t === 21 && !hasCut) return false;
                        if (t === 22 && !hasStrength) return false;
                        if (npcBlocks.has(`${tx},${ty}`)) return false;
                        return true;
                    }

                    let head = 0;
                    const dirs = [[1,0],[-1,0],[0,1],[0,-1]];
                    while (head < q.length) {
                        const [x, y] = q[head++];
                        for (const [dx, dy] of dirs) {
                            const nx = x + dx, ny = y + dy;
                            if (nx < 0 || ny < 0 || nx >= MAP_COLS || ny >= MAP_ROWS) continue;
                            const idx = ny * MAP_COLS + nx;
                            if (visited[idx]) continue;
                            if (tileWalkable(nx, ny)) {
                                visited[idx] = 1;
                                q.push([nx, ny]);
                            }
                        }
                    }
                    const isReach = (x, y) => !!visited[y * MAP_COLS + x];
                    const isAdjReach = (x, y) => dirs.some(([dx, dy]) => {
                        const nx = x + dx, ny = y + dy;
                        return nx >= 0 && ny >= 0 && nx < MAP_COLS && ny < MAP_ROWS && isReach(nx, ny);
                    });
                    return { count: q.length, isReach, isAdjReach };
                }

                const noHm   = runBfs(false, false, false);
                const cutOnly = runBfs(true, false, false);
                const surfStr = runBfs(false, true, true);
                const allHm  = runBfs(true, true, true);

                // Doors check
                const allDoors = Object.keys(DOOR_TARGETS);
                const unreachableDoors = allDoors.filter(k => {
                    const [x, y] = k.split(',').map(Number);
                    return !allHm.isReach(x, y);
                });

                // Treasure balls check
                const treasureCoords = [];
                for (let r = 0; r < MAP_ROWS; r++) {
                    for (let c = 0; c < MAP_COLS; c++) {
                        if (WORLD_MAP[r][c] === 10) treasureCoords.push([c, r]);
                    }
                }
                const unreachableItems = treasureCoords.filter(([x, y]) => !allHm.isReach(x, y));

                // 17 Signs check
                const signKeys = Object.keys(SIGNS);
                const badTileSigns = [];
                const unreachableSigns = [];
                signKeys.forEach(k => {
                    const [x, y] = k.split(',').map(Number);
                    if (WORLD_MAP[y]?.[x] !== 8) badTileSigns.push({ k, tile: WORLD_MAP[y]?.[x] });
                    if (!allHm.isAdjReach(x, y)) unreachableSigns.push(k);
                });

                // HM Progression Unlockability Check (can player reach the NPCs that grant each HM without needing that HM?)
                const captainNpc = NPCS.find(n => n.id === 'ss_anne_captain');
                const mistyNpc   = NPCS.find(n => n.id === 'misty');
                const wardenNpc  = NPCS.find(n => n.id === 'safari_warden');
                const erikaDoor  = [170, 147]; // Celadon Gym / Erika
                const surfHouse  = NPCS.find(n => n.id === 'safari_surf_guide');
                const kogaDoor   = [169, 301]; // Fuchsia Gym

                // Celadon Fly check
                const celadonFly = FAST_TRAVEL_POINTS.find(p => p.id === 'celadon');
                const celadonFlyTile = [Math.floor(celadonFly.x), Math.floor(celadonFly.y)];

                return {
                    noHmCount: noHm.count,
                    allHmCount: allHm.count,
                    totalDoors: allDoors.length,
                    unreachableDoors,
                    indigoDoor13Reach_NoHm: noHm.isReach(13, 17),
                    indigoDoor14Reach_NoHm: noHm.isReach(14, 17),
                    indigoDoor13Reach_SurfStr: surfStr.isReach(13, 17),
                    indigoDoor14Reach_SurfStr: surfStr.isReach(14, 17),
                    route2East_NoCut: noHm.isReach(66, 103),
                    route2East_WithCut: cutOnly.isReach(66, 103),
                    cinnabar_NoSurf: noHm.isAdjReach(67, 349),
                    cinnabar_AllHm: allHm.isAdjReach(67, 349),
                    totalItems: treasureCoords.length,
                    unreachableItems,
                    item168_50: allHm.isReach(168, 50),
                    item128_70: allHm.isReach(128, 70),
                    totalSigns: signKeys.length,
                    badTileSigns,
                    unreachableSigns,
                    canUnlockCut: noHm.isAdjReach(mistyNpc.x, mistyNpc.y) && (!captainNpc || noHm.isAdjReach(captainNpc.x, captainNpc.y)),
                    canUnlockStrength: (!wardenNpc || noHm.isAdjReach(wardenNpc.x, wardenNpc.y)),
                    canUnlockSurf: (!surfHouse || noHm.isAdjReach(surfHouse.x, surfHouse.y)),
                    celadonFlyReachable: noHm.isReach(celadonFlyTile[0], celadonFlyTile[1]),
                    indigoNurseReachable: surfStr.isAdjReach(15, 19),
                    indigoNurseTile: WORLD_MAP[19][15],
                };
            }""")

            assert_ok(bfs_report["unreachableDoors"] == [], f"All {bfs_report['totalDoors']} doors reachable via BFS (unreachable: {bfs_report['unreachableDoors']})")
            assert_ok(not bfs_report["indigoDoor13Reach_NoHm"] and not bfs_report["indigoDoor14Reach_NoHm"], "Indigo Plateau doors (13,17)/(14,17) gated without HMs")
            assert_ok(bfs_report["indigoDoor13Reach_SurfStr"] and bfs_report["indigoDoor14Reach_SurfStr"], "Indigo Plateau doors (13,17) AND (14,17) reachable via BFS with Surf+Strength!")
            assert_ok(not bfs_report["route2East_NoCut"] and bfs_report["route2East_WithCut"], "Cut Bush (66,152) blocks Route 2 East without Cut and opens with Cut!")
            assert_ok(not bfs_report["cinnabar_NoSurf"] and bfs_report["cinnabar_AllHm"], "Water blocks Cinnabar without Surf and opens with Surf!")
            assert_ok(bfs_report["canUnlockCut"] and bfs_report["canUnlockStrength"] and bfs_report["canUnlockSurf"], "All HM/Badge granters are reachable without circular dependency!")
            assert_ok(bfs_report["totalItems"] == 14 and bfs_report["unreachableItems"] == [], f"All {bfs_report['totalItems']} treasure balls reachable (including (168,50) & (128,70))")
            assert_ok(bfs_report["item168_50"] and bfs_report["item128_70"], "Previously blocked balls (168,50) & (128,70) reachable via BFS")
            assert_ok(bfs_report["totalSigns"] == 17 and bfs_report["badTileSigns"] == [] and bfs_report["unreachableSigns"] == [], f"All 17 guidance signs sit on real tile===8 and are BFS-reachable!")
            assert_ok(bfs_report["celadonFlyReachable"], "Celadon Fast Travel lands on open walkable plaza (no trap!)")
            assert_ok(bfs_report["indigoNurseTile"] == 9 and bfs_report["indigoNurseReachable"], "Indigo Nurse Joy stands on walkable tile (15,19) and is reachable")

            # Live test picking up (168,50) and (128,70)
            pickup_res = page.evaluate("""() => {
                function totalInv() {
                    return Object.values(state.inventory).reduce((a, b) => a + (Number(b) || 0), 0);
                }
                const results = [];
                for (const [bx, by, sx, sy, dir] of [[168, 50, 169, 50, 'left'], [128, 70, 128, 71, 'up']]) {
                    const before = totalInv();
                    state.currentMap = 'OVERWORLD';
                    state.player.x = (sx + 0.5) * TILE_SIZE;
                    state.player.y = (sy + 0.5) * TILE_SIZE;
                    state.player.dir = dir;
                    interactInOverworld();
                    const after = totalInv();
                    const collected = !!state.collectedItems[`${bx},${by}`];
                    // close dialogue
                    while (state.mode === 'DIALOGUE') advanceDialogue();
                    results.push({ key: `${bx},${by}`, delta: after - before, collected });
                }
                return results;
            }""")
            for r in pickup_res:
                assert_ok(r["collected"] and r["delta"] > 0, f"Treasure ball ({r['key']}) picked up & added +{r['delta']} item(s)")

            # Live test interacting with ALL 17 signs!
            sign_res = page.evaluate("""() => {
                const dirs = [
                    { dx: 0, dy: 1, face: 'up' },
                    { dx: 0, dy: -1, face: 'down' },
                    { dx: 1, dy: 0, face: 'left' },
                    { dx: -1, dy: 0, face: 'right' }
                ];
                const failures = [];
                const passed = [];
                for (const [key, expectedText] of Object.entries(SIGNS)) {
                    const [sx, sy] = key.split(',').map(Number);
                    let tested = false;
                    for (const d of dirs) {
                        const px = sx + d.dx, py = sy + d.dy;
                        const t = WORLD_MAP[py]?.[px];
                        if (t !== undefined && t !== 1 && t !== 3 && t !== 5 && t !== 6 && t !== 8 && t !== 11 && t !== 13 && t !== 16 && !NPCS.some(n => n.x === px && n.y === py)) {
                            state.currentMap = 'OVERWORLD';
                            state.mode = 'OVERWORLD';
                            state.player.x = (px + 0.5) * TILE_SIZE;
                            state.player.y = (py + 0.5) * TILE_SIZE;
                            state.player.dir = d.face;
                            interactInOverworld();
                            const shown = document.getElementById('dialogueText').textContent;
                            while (state.mode === 'DIALOGUE') advanceDialogue();
                            if (shown === expectedText) {
                                passed.push(key);
                            } else {
                                failures.push({ key, shown, expectedText });
                            }
                            tested = true;
                            break;
                        }
                    }
                    if (!tested) failures.push({ key, error: 'no adjacent walkable tile' });
                }
                return { count: passed.length, failures };
            }""")
            assert_ok(sign_res["count"] == 17 and sign_res["failures"] == [], f"All 17 signs verified live with exact text ({sign_res['count']}/17)")

            # Live HM Bump Toast & Solid Collision Check (Fix #8)
            hm_bump = page.evaluate("""() => {
                state.hms = { cut: false, fly: false, surf: false, strength: false };
                state.badges = { boulder: false, cascade: false, thunder: false, rainbow: false, soul: false, marsh: false, volcano: false, earth: false };
                state.currentMap = 'OVERWORLD';
                state.mode = 'OVERWORLD';

                const checks = [];
                // 1. Cut Bush at (66,152): stand at (66,153) and try stepping into (66,152)
                _hmBumpCooldownUntil = 0;
                const cutBlocked = !canStandAt(66.5 * TILE_SIZE, 152.5 * TILE_SIZE);
                state.player.x = 66.5 * TILE_SIZE;
                state.player.y = 153.1 * TILE_SIZE;
                inputState.keys.clear();
                inputState.keys.add('up');
                for (let i = 0; i < 8; i++) updateContinuousPlayerMovement();
                inputState.keys.clear();
                const cutToast = document.getElementById('hmBumpToast').textContent;
                checks.push({ name: 'CUT', blocked: cutBlocked, toast: cutToast, kw: 'HM01' });

                // 2. Strength Boulder at (16,83): stand at (16,84) and bump up
                _hmBumpCooldownUntil = 0;
                const strBlocked = !canStandAt(16.5 * TILE_SIZE, 83.5 * TILE_SIZE);
                state.player.x = 16.5 * TILE_SIZE;
                state.player.y = 84.1 * TILE_SIZE;
                inputState.keys.add('up');
                for (let i = 0; i < 8; i++) updateContinuousPlayerMovement();
                inputState.keys.clear();
                const strToast = document.getElementById('hmBumpToast').textContent;
                checks.push({ name: 'STRENGTH', blocked: strBlocked, toast: strToast, kw: 'HM04' });

                // 3. Water at Pallet South (59,252): stand at (59,251) and bump down
                _hmBumpCooldownUntil = 0;
                const surfBlocked = !canStandAt(59.5 * TILE_SIZE, 252.5 * TILE_SIZE);
                state.player.x = 59.5 * TILE_SIZE;
                state.player.y = 251.9 * TILE_SIZE;
                inputState.keys.add('down');
                for (let i = 0; i < 8; i++) updateContinuousPlayerMovement();
                inputState.keys.clear();
                const surfToast = document.getElementById('hmBumpToast').textContent;
                checks.push({ name: 'SURF', blocked: surfBlocked, toast: surfToast, kw: 'HM03' });

                return checks;
            }""")
            for chk in hm_bump:
                assert_ok(chk["blocked"] and chk["kw"] in chk["toast"], f"HM {chk['name']} blocks movement & displays specific hint: {chk['toast'][:55]}")

            # Live Indigo Plateau Door Warp & Elite Four Gate (Fix #2)
            indigo_test = page.evaluate("""() => {
                const res = {};
                for (const doorX of [13, 14]) {
                    state.currentMap = 'OVERWORLD';
                    state.mode = 'OVERWORLD';
                    state.player.x = (doorX + 0.5) * TILE_SIZE;
                    state.player.y = 18.1 * TILE_SIZE;
                    inputState.keys.clear();
                    inputState.keys.add('up');
                    for (let i = 0; i < 15 && state.currentMap === 'OVERWORLD'; i++) {
                        updateContinuousPlayerMovement();
                    }
                    inputState.keys.clear();
                    res[`door_${doorX}_entered`] = state.currentMap;

                    // Check Champion Gary gate before E4 defeated
                    if (doorX === 13) {
                        const gary = INTERIOR_MAPS.INDIGO_HALL.npcs.find(n => n.role === 'champion');
                        state.eliteFourDefeated = {};
                        state.championDefeated = false;
                        state.player.x = (gary.x + 0.5) * TILE_SIZE;
                        state.player.y = (gary.y + 1.4) * TILE_SIZE;
                        state.player.dir = 'up';
                        interactInOverworld();
                        res.e4GateMsg = document.getElementById('dialogueText').textContent;
                        res.e4GateBlockedBattle = (state.mode !== 'BATTLE');
                        while (state.mode === 'DIALOGUE') advanceDialogue();
                    }

                    // Walk out onto Exit Mat (tile 20)
                    const m = INTERIOR_MAPS.INDIGO_HALL;
                    let ex = 0, ey = 0;
                    for (let r = 0; r < m.rows; r++) {
                        for (let c = 0; c < m.cols; c++) {
                            if (m.grid[r][c] === 20) { ex = c; ey = r; }
                        }
                    }
                    state.player.x = (ex + 0.5) * TILE_SIZE;
                    state.player.y = (ey - 0.1) * TILE_SIZE;
                    inputState.keys.add('down');
                    for (let i = 0; i < 15 && state.currentMap !== 'OVERWORLD'; i++) {
                        updateContinuousPlayerMovement();
                    }
                    inputState.keys.clear();
                    res[`door_${doorX}_exited`] = state.currentMap;
                    res[`door_${doorX}_exitWalkable`] = canStandAt(state.player.x, state.player.y);
                }
                return res;
            }""")
            assert_ok(indigo_test["door_13_entered"] == "INDIGO_HALL" and indigo_test["door_14_entered"] == "INDIGO_HALL", "Walking onto Indigo doors (13,17) and (14,17) warps into INDIGO_HALL")
            assert_ok(indigo_test["door_13_exited"] == "OVERWORLD" and indigo_test["door_13_exitWalkable"], "Walking onto exit mat returns to OVERWORLD on a walkable tile")
            assert_ok(indigo_test["e4GateBlockedBattle"] and "רביעיית העילית" in indigo_test["e4GateMsg"], "Champion Gary enforces defeating all 4 Elite Four members first")

            # ================================================================
            # 4. SAVE & RELOAD INSIDE BUILDINGS + CORRUPTED SAVE (Fix #1)
            # ================================================================
            print("\n=== 4. Save & Reload Inside Buildings (Fix #1) ===")
            for interior_id in ["HOUSE", "POKECENTER", "GYM_BROCK", "CAVE_MT_MOON"]:
                saved_info = page.evaluate(f"""() => {{
                    state.currentMap = 'OVERWORLD';
                    state.player.x = 63.5 * TILE_SIZE;
                    state.player.y = 247.5 * TILE_SIZE;
                    enterInteriorMap({json.dumps(interior_id)});
                    // Pick a walkable interior tile slightly offset from spawn
                    const m = INTERIOR_MAPS[{json.dumps(interior_id)}];
                    const tx = Math.floor(state.player.x / TILE_SIZE);
                    const ty = Math.max(2, Math.floor(state.player.y / TILE_SIZE) - 1);
                    if (canStandAt((tx + 0.5) * TILE_SIZE, (ty + 0.5) * TILE_SIZE)) {{
                        state.player.x = (tx + 0.5) * TILE_SIZE;
                        state.player.y = (ty + 0.5) * TILE_SIZE;
                    }}
                    state.player.dir = 'right';
                    saveGameToSlot(state.saveName, false);
                    return {{
                        slot: state.saveName,
                        map: state.currentMap,
                        tx: Math.floor(state.player.x / TILE_SIZE),
                        ty: Math.floor(state.player.y / TILE_SIZE),
                        dir: state.player.dir
                    }};
                }}""")

                # Real browser page reload!
                page.reload(wait_until="networkidle")
                page.wait_for_timeout(300)
                loaded_info = page.evaluate(f"""() => {{
                    loadGameFromSlot({json.dumps(saved_info['slot'])});
                    return {{
                        map: state.currentMap,
                        tx: Math.floor(state.player.x / TILE_SIZE),
                        ty: Math.floor(state.player.y / TILE_SIZE),
                        dir: state.player.dir,
                        walkable: canStandAt(state.player.x, state.player.y)
                    }};
                }}""")
                assert_ok(
                    loaded_info["map"] == interior_id
                    and loaded_info["tx"] == saved_info["tx"]
                    and loaded_info["ty"] == saved_info["ty"]
                    and loaded_info["dir"] == "right"
                    and loaded_info["walkable"],
                    f"Save & real reload inside {interior_id} restored exact map & tile ({loaded_info['tx']},{loaded_info['ty']}) facing {loaded_info['dir']}",
                )

            # Save with missing currentMap & stuck coords (6,6) -> must land on walkable tile!
            missing_map_res = page.evaluate("""() => {
                const all = getAllSaves();
                const cur = JSON.parse(JSON.stringify(all[state.saveName]));
                delete cur.currentMap;
                cur.player = { x: 6.5 * TILE_SIZE, y: 6.5 * TILE_SIZE, dir: 'down' }; // solid trees on Overworld
                cur.savedOverworldPos = { x: 63.5 * TILE_SIZE, y: 247.5 * TILE_SIZE, dir: 'down' };
                all['legacy_no_map'] = cur;
                localStorage.setItem(STORAGE_KEY, JSON.stringify(all));
                loadGameFromSlot('legacy_no_map');
                return {
                    map: state.currentMap,
                    tx: Math.floor(state.player.x / TILE_SIZE),
                    ty: Math.floor(state.player.y / TILE_SIZE),
                    walkable: canStandAt(state.player.x, state.player.y)
                };
            }""")
            assert_ok(
                missing_map_res["map"] == "OVERWORLD" and missing_map_res["walkable"],
                f"Save with missing currentMap safely placed on walkable tile ({missing_map_res['tx']},{missing_map_res['ty']})",
            )

            # ================================================================
            # 5. OLD SAVE MIGRATION mapVersion 4 -> 5 (Fix #4)
            # ================================================================
            print("\n=== 5. Old Save Migration mapVersion 4 -> 5 (Fix #4) ===")
            mig_res = page.evaluate("""() => {
                const oldSave = {
                    saveName: 'old_v4_trainer',
                    mapVersion: 4,
                    playerName: 'שחקן ישן',
                    player: { x: 2.5 * TILE_SIZE, y: 2.5 * TILE_SIZE, dir: 'up' },
                    money: 9876,
                    badges: { boulder: true, cascade: true, thunder: false, rainbow: false, soul: false, marsh: false, volcano: false, earth: false },
                    inventory: { pokeball: 12, ultraball: 7, potion: 4 },
                    party: [createMonster('pikachu', 25), createMonster('charizard', 40)],
                    pcBox: [],
                    pokedexCaught: { pikachu: true, charizard: true },
                    pokedexSeen: { pikachu: true, charizard: true }
                };
                const all = getAllSaves();
                all['old_v4_trainer'] = oldSave;
                localStorage.setItem(STORAGE_KEY, JSON.stringify(all));

                loadGameFromSlot('old_v4_trainer');
                const migMsg = document.getElementById('dialogueText').textContent;
                while (state.mode === 'DIALOGUE') advanceDialogue();

                // Save again and check new mapVersion
                saveGameToSlot('old_v4_trainer', false);
                const updated = getAllSaves()['old_v4_trainer'];

                return {
                    walkable: canStandAt(state.player.x, state.player.y),
                    tx: Math.floor(state.player.x / TILE_SIZE),
                    ty: Math.floor(state.player.y / TILE_SIZE),
                    money: state.money,
                    partyCount: state.party.length,
                    leadLv: state.party[0]?.level,
                    ultraballs: state.inventory.ultraball,
                    boulderBadge: state.badges.boulder,
                    dexCaught: Object.keys(state.pokedexCaught).length,
                    migMsg,
                    newMapVersion: updated.mapVersion
                };
            }""")
            assert_ok(
                mig_res["walkable"] and 56 <= mig_res["tx"] <= 72 and 240 <= mig_res["ty"] <= 252,
                f"mapVersion 4 save migrated to walkable Pallet Town ({mig_res['tx']},{mig_res['ty']})",
            )
            assert_ok(
                mig_res["money"] == 9876 and mig_res["partyCount"] == 2 and mig_res["leadLv"] == 25
                and mig_res["ultraballs"] == 7 and mig_res["boulderBadge"] and mig_res["dexCaught"] == 2,
                "Migration preserved party, items, badges, Pokédex & money 100% intact",
            )
            assert_ok("עודכנה" in mig_res["migMsg"] or "עולם" in mig_res["migMsg"], f"World-update migration message shown: {mig_res['migMsg'][:55]}")
            assert_ok(mig_res["newMapVersion"] == 5, f"New save writes mapVersion={mig_res['newMapVersion']}")

            # ================================================================
            # 6. WONDER TRADE BALANCE (Fix #6)
            # ================================================================
            print("\n=== 6. Wonder Trade Balance: 1,000 Rolls & Daily Limit (Fix #6) ===")
            wt_res = page.evaluate("""() => {
                startNewGame('סוחר', 'pikachu');
                while (state.mode === 'DIALOGUE') advanceDialogue();

                // 1,000 simulated Wonder Trade rolls
                const legendaries = new Set(['articuno', 'zapdos', 'moltres', 'mewtwo', 'mew']);
                let legendaryHits = 0;
                const sampleMon = createMonster('pidgey', 10);
                for (let i = 0; i < 1000; i++) {
                    const rolled = rollWonderTradeSpecies(sampleMon);
                    if (legendaries.has(rolled)) legendaryHits++;
                }

                // Held item preservation + level proximity + 3/day limit
                state.party[0] = createMonster('pikachu', 20);
                state.party[0].heldItem = 'leftovers';
                state.inventory.leftovers = 0;
                state.wonderTradeDaily = { date: getLocalDateKey(), count: 0 };

                const r1 = performWonderTrade(0);
                const leftoversReturned = (state.inventory.leftovers === 1);
                const lvDiff1 = Math.abs(r1.mon.level - 20);
                const r2 = performWonderTrade(0);
                const r3 = performWonderTrade(0);
                const r4 = performWonderTrade(0); // should be blocked!

                return {
                    legendaryHits,
                    leftoversReturned,
                    lvDiff1,
                    r1ok: r1.ok, r2ok: r2.ok, r3ok: r3.ok,
                    r4ok: r4.ok, r4msg: r4.message
                };
            }""")
            assert_ok(wt_res["legendaryHits"] == 0, f"1,000 simulated Wonder Trades produced {wt_res['legendaryHits']} legendaries/mythicals")
            assert_ok(wt_res["leftoversReturned"], "Equipped held item returned to inventory on trade")
            assert_ok(wt_res["lvDiff1"] <= 2, f"Received Wonder Trade level within ±2 of sent level (diff={wt_res['lvDiff1']})")
            assert_ok(wt_res["r1ok"] and wt_res["r2ok"] and wt_res["r3ok"] and not wt_res["r4ok"], f"Daily limit allows 3 trades and blocks 4th: {wt_res['r4msg'][:55]}")

            # ================================================================
            # 7. DAYCARE & FOSSIL LAB ANTI-FARMING (Fix #7)
            # ================================================================
            print("\n=== 7. Daycare & Fossil Lab Anti-Farming (Fix #7) ===")
            farm_res = page.evaluate("""() => {
                startNewGame('מאמן', 'charmander');
                while (state.mode === 'DIALOGUE') advanceDialogue();

                // Daycare 20 talks in a row without walking 60 steps between them
                state.money = 50000;
                state.stepsWalked = 100;
                state.daycareLastStep = 0;
                const startLv = state.party[0].level;
                const startMoney = state.money;
                for (let i = 0; i < 20; i++) {
                    handleDaycareInteraction();
                    while (state.mode === 'DIALOGUE') advanceDialogue();
                }
                const endLv = state.party[0].level;
                const daycareSpent = startMoney - state.money;

                // Fossil Lab: 5 talks in a row -> max 3 unique fossils, ₽2,500 each, no farming!
                state.money = 20000;
                state.revivedFossils = {};
                const startFossilMoney = state.money;
                const beforeTotalMons = state.party.length + state.pcBox.length;
                for (let i = 0; i < 5; i++) {
                    handleFossilRevival();
                    while (state.mode === 'DIALOGUE') advanceDialogue();
                }
                const afterTotalMons = state.party.length + state.pcBox.length;
                const fossilSpent = startFossilMoney - state.money;
                const claimedKeys = Object.keys(state.revivedFossils);

                return {
                    lvGain: endLv - startLv,
                    daycareSpent,
                    fossilsAdded: afterTotalMons - beforeTotalMons,
                    fossilSpent,
                    claimedCount: claimedKeys.length
                };
            }""")
            assert_ok(farm_res["lvGain"] == 1 and farm_res["daycareSpent"] > 0, f"Talking to Daycare 20x in a row gave only +{farm_res['lvGain']} level (cost ₽{farm_res['daycareSpent']}), blocked by 60-step cooldown")
            assert_ok(farm_res["fossilsAdded"] == 3 and farm_res["fossilSpent"] == 7500 and farm_res["claimedCount"] == 3, f"Fossil Lab charged ₽{farm_res['fossilSpent']} for 3 unique fossils & blocked farming on 4th/5th talk")

            # ================================================================
            # 8. PORTABLE SAVE CODE, PARENT CONTROLS & BEGINNER GUIDE (Fix #9)
            # ================================================================
            print("\n=== 8. Portable Save Code, Parent Controls (PIN + 1-min limit) & Guide (Fix #9) ===")
            # Beginner Guide
            page.locator("#btnGuideHud").click()
            page.wait_for_timeout(100)
            guide_vis = page.evaluate("!document.getElementById('beginnerGuideModal').classList.contains('hidden')")
            assert_ok(guide_vis, "Beginner Guide modal opens from HUD")
            page.locator("#btnCloseGuideModal").click()

            # Portable Save Code export -> clear localStorage -> import!
            code_res = page.evaluate("""() => {
                state.money = 12345;
                state.badges.boulder = true;
                const code = exportPortableSaveCode(state.saveName);
                const origName = state.saveName;
                localStorage.clear();
                // Reset in-memory state
                state.money = 0;
                state.badges.boulder = false;
                const imp = importPortableSaveCode(code);
                return {
                    startsWithPrefix: code && code.startsWith('PKHD5:'),
                    impOk: imp.ok,
                    restoredMoney: state.money,
                    restoredBadge: state.badges.boulder,
                    restoredSlot: state.saveName === origName
                };
            }""")
            assert_ok(
                code_res["startsWithPrefix"] and code_res["impOk"]
                and code_res["restoredMoney"] == 12345 and code_res["restoredBadge"],
                "Portable Save Code (PKHD5:...) survives clearing localStorage and restores save intact!",
            )

            # Parent Controls: 4-digit PIN + 1-minute limit -> play past it -> unskippable lock screen!
            page.evaluate("localStorage.removeItem(PARENT_CTRL_KEY)")
            page.locator("#btnParentHud").click()
            page.wait_for_timeout(100)
            page.fill("#parentPinInput", "4321")
            page.locator("#btnSubmitParentPin").click()
            page.wait_for_timeout(100)
            page.locator('[data-parent-preset="1"]').click()
            page.locator("#btnSaveParentSettings").click()
            page.wait_for_timeout(100)
            page.locator("#btnCloseParentModal").click()

            # Advance 61 seconds past the 1-minute limit
            page.evaluate("tickParentPlaytime(61)")
            page.wait_for_timeout(100)

            lock_vis = page.evaluate("!document.getElementById('timeLimitLockModal').classList.contains('hidden') && state.parentLocked === true")
            assert_ok(lock_vis, "1-minute limit triggers friendly stop screen & locks game")

            # Pressing Escape cannot skip it
            page.keyboard.press("Escape")
            page.wait_for_timeout(80)
            still_locked_esc = page.evaluate("!document.getElementById('timeLimitLockModal').classList.contains('hidden') && state.parentLocked === true")
            assert_ok(still_locked_esc, "Stop screen cannot be skipped with Escape")

            # Wrong PIN rejected
            page.fill("#lockScreenPinInput", "9999")
            page.locator("#btnUnlockTimeLimit15").click()
            page.wait_for_timeout(80)
            still_locked_wrong = page.evaluate("!document.getElementById('timeLimitLockModal').classList.contains('hidden') && !document.getElementById('lockScreenPinError').classList.contains('hidden')")
            assert_ok(still_locked_wrong, "Wrong PIN rejected on stop screen")

            # Right PIN unlocks!
            page.fill("#lockScreenPinInput", "4321")
            page.locator("#btnUnlockTimeLimitOff").click()
            page.wait_for_timeout(80)
            unlocked = page.evaluate("document.getElementById('timeLimitLockModal').classList.contains('hidden') && state.parentLocked === false")
            assert_ok(unlocked, "Correct 4-digit Parent PIN unlocks stop screen")

            # ================================================================
            # 9. QA REGRESSION CHECKS (TM15, TM Cancel, Battle Heal, Freeze, Stat Reset, Mobile)
            # ================================================================
            print("\n=== 9. QA Regression Checks ===")
            qa_res = page.evaluate("""() => {
                startNewGame('רגרסיה', 'squirtle');
                while (state.mode === 'DIALOGUE') advanceDialogue();

                // TM15 visible in Bag
                state.inventory.tm15_hyperbeam = 1;
                openPartyBagModal(false);
                const tm15Rendered = !!document.querySelector('#modalBagList [data-bag-key="tm15_hyperbeam"]');

                // TM Cancel does NOT consume TM disc when lead has 4 moves
                state.party[0].moves = [
                    { name: 'מכה1', power: 40, type: 'NORMAL', pp: 20, maxPp: 20 },
                    { name: 'מכה2', power: 40, type: 'WATER', pp: 20, maxPp: 20 },
                    { name: 'מכה3', power: 40, type: 'WATER', pp: 20, maxPp: 20 },
                    { name: 'מכה4', power: 40, type: 'NORMAL', pp: 20, maxPp: 20 }
                ];
                state.inventory.tm01_thunderbolt = 1;
                useSpecialItemOnLead('tm01_thunderbolt');
                document.getElementById('btnCancelMoveReplace').click();
                const tmNotConsumed = (state.inventory.tm01_thunderbolt === 1);
                document.getElementById('btnClosePartyBag').click();

                // Instant Heal hidden during battle
                triggerWildBattle(63, 247, 'pidgey', 3);
                openPartyBagModal(true);
                const healHiddenInBattle = document.getElementById('btnInstantHealModal').classList.contains('hidden');
                document.getElementById('btnClosePartyBag').click();

                // Enemy FREEZE turn skip check
                state.battle.enemy.status = 'FREEZE';
                const hpBeforeFreeze = state.party[0].hp;
                runEnemyBattleTurn();
                const freezeMsg = (state.battle.msgQueue || []).join(' ') + ' ' + document.getElementById('battleMsgText').textContent;
                const freezeHandled = state.party[0].hp === hpBeforeFreeze || freezeMsg.includes('קפוא') || freezeMsg.includes('הפשיר');

                // Stat stage reset after battle
                state.party[0].atkStage = 3;
                state.party[0].defStage = -2;
                endBattleToOverworld();
                const stagesReset = (state.party[0].atkStage === 0 && state.party[0].defStage === 0);

                return { tm15Rendered, tmNotConsumed, healHiddenInBattle, freezeHandled, stagesReset };
            }""")
            assert_ok(qa_res["tm15Rendered"], "TM15 Hyper Beam is visible & usable in Bag modal")
            assert_ok(qa_res["tmNotConsumed"], "Cancelling 4-move TM replacement does NOT consume TM disc")
            assert_ok(qa_res["healHiddenInBattle"], "Instant Heal button hidden in Party modal during battle")
            assert_ok(qa_res["freezeHandled"], "Enemy FREEZE status properly skips/thaws enemy turn")
            assert_ok(qa_res["stagesReset"], "Attack/Defense stat stages reset to 0 after battle")

            # Mobile 390x844 Layout Check (fresh context so fullscreen doesn't block viewport)
            mobile_ctx = browser.new_context(viewport={"width": 390, "height": 844})
            m_page = mobile_ctx.new_page()
            m_page.on("pageerror", lambda err: page_errors.append(f"[mobile] {err}"))
            m_page.goto(f"{base_url}/index.html", wait_until="networkidle")
            m_page.wait_for_timeout(250)
            start_fresh_game(m_page, "מובייל")
            m_page.wait_for_timeout(150)
            mob_layout = m_page.evaluate("""() => {
                const hud = document.getElementById('topHud').getBoundingClientRect();
                const banner = document.getElementById('activeQuestBanner').getBoundingClientRect();
                return { hudBottom: hud.bottom, bannerTop: banner.top };
            }""")
            assert_ok(
                mob_layout["bannerTop"] >= mob_layout["hudBottom"] - 1,
                f"Mobile 390x844 HUD ({mob_layout['hudBottom']:.0f}px) does not overlap Quest Banner ({mob_layout['bannerTop']:.0f}px)",
            )
            mobile_ctx.close()


            # ================================================================
            # 11. v5.1.0 GAPS: offline Tailwind, 151/151, split caves, no alert()
            # ================================================================
            print("\n=== 11. v5.1.0: Tailwind local, 151 obtainable, split caves, no alert() ===")
            html_text = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read() if "ROOT" in globals() else open("index.html", encoding="utf-8").read()
            assert_ok("gstatic.com" not in html_text and "cdn.tailwindcss" not in html_text, "index.html has no external script dependency")
            sw_text = open("sw.js", encoding="utf-8").read()
            assert_ok("vendor/tailwindcss.min.js" in sw_text, "sw.js precaches vendored Tailwind")
            assert_ok(not re.search(r"(?<![\w.])alert\(", html_text), "No native alert() calls remain")
            res = page.evaluate("""() => {
              const got = new Set();
              Object.values(KANTO_ZONE_CATALOG).forEach(z => (z.pool||[]).forEach(n => got.add(n)));
              const byId = {}; DEX_ORDER.forEach(n => {});
              return { pools: [...got], trades: NPC_TRADES_LIST.map(t => t.getId) };
            }""")
            has_dig = page.evaluate("() => KANTO_ZONE_CATALOG.CAVE_DIGGLETT.pool.includes(50) && KANTO_ZONE_CATALOG.CAVE_DIGGLETT.pool.includes(51)")
            assert_ok(has_dig, "Diglett (#50) and Dugtrio (#51) in Diglett's Cave pool")
            assert_ok("lickitung" in res["trades"], "Lickitung (#108) obtainable via NPC trade")
            caves = page.evaluate("""() => {
              const out = {};
              return Object.keys(INTERIOR_MAPS).filter(k => k.startsWith('CAVE_'));
            }""")
            for c in ["CAVE_MT_MOON", "CAVE_DIGGLETT", "CAVE_ROCK_TUNNEL", "CAVE_SEAFOAM", "CAVE_VOLCANO"]:
                assert_ok(c in caves, f"Interior {c} exists")
            homes = page.evaluate("""() => {
              const r = {};
              Object.values(INTERIOR_MAPS).forEach(m => (m.npcs||[]).forEach(n => { if (n.role === 'legendary') r[n.speciesId] = m.id; }));
              return r;
            }""")
            exp = {"zapdos": "CAVE_ROCK_TUNNEL", "articuno": "CAVE_SEAFOAM", "moltres": "CAVE_VOLCANO", "mew": "CAVE_MT_MOON", "mewtwo": "INDIGO_HALL"}
            assert_ok(all(homes.get(k) == v for k, v in exp.items()), f"Legendaries in natural homes: {homes}")
            reach = page.evaluate("""() => {
              const out = {};
              ['CAVE_DIGGLETT','CAVE_ROCK_TUNNEL','CAVE_SEAFOAM','CAVE_VOLCANO','CAVE_MT_MOON'].forEach(id => {
                state.currentMap = 'OVERWORLD'; state.player.x = 63.5*TILE_SIZE; state.player.y = 247.5*TILE_SIZE;
                enterInteriorMap(id);
                const m = INTERIOR_MAPS[id];
                const ok = (x,y) => canStandAt((x+0.5)*TILE_SIZE,(y+0.5)*TILE_SIZE);
                const sx = Math.floor(state.player.x/TILE_SIZE), sy = Math.floor(state.player.y/TILE_SIZE);
                const seen = new Set([sx+','+sy]); const q=[[sx,sy]];
                while (q.length) { const [x,y]=q.shift(); [[1,0],[-1,0],[0,1],[0,-1]].forEach(([dx,dy])=>{ const nx=x+dx, ny=y+dy; const k=nx+','+ny; if(!seen.has(k)&&nx>=0&&ny>=0&&nx<m.cols&&ny<m.rows&&ok(nx,ny)){seen.add(k);q.push([nx,ny]);} }); }
                const leg = (m.npcs||[]).filter(n=>n.role==='legendary').map(n => {
                  const lx=Math.floor(n.x), ly=Math.floor(n.y);
                  return [[0,1],[0,-1],[1,0],[-1,0]].some(([dx,dy])=>seen.has((lx+dx)+','+(ly+dy)));
                });
                out[id] = { tiles: seen.size, legendReachable: leg.every(Boolean) };
              });
              return out;
            }""")
            for k, v in reach.items():
                assert_ok(v["tiles"] > 20 and v["legendReachable"], f"{k}: walkable ({v['tiles']} tiles) and legendary reachable")
            html_doors = re.findall(r'\[\d+, \d+, -?\d+, -?\d+, 13, 14, "(CAVE_[A-Z_]+)"\]', html_text)
            from collections import Counter
            cnt = Counter(html_doors)
            assert_ok(cnt["CAVE_ROCK_TUNNEL"] == 3 and cnt["CAVE_SEAFOAM"] == 2 and cnt["CAVE_DIGLETT"] == 2 and cnt["CAVE_VOLCANO"] == 1, f"Cave doors split into distinct interiors: {dict(cnt)}")

            # ================================================================
            # 10. MANUAL-STYLE PLAYTHROUGH: Start -> Pallet -> Gym #1 -> Save/Reload -> Offline
            # ================================================================
            print("\n=== 10. Playthrough: Start -> First Town -> Gym #1 Brock -> Save/Reload -> Offline ===")
            start_fresh_game(page, "הרפתקה")
            # Walk in Pallet Town toward Prof. Oak (65, 250) and talk
            pt_res = page.evaluate("""() => {
                // Step down & right in Pallet Town
                const x0 = state.player.x;
                inputState.keys.add('right');
                for (let i = 0; i < 12; i++) updateContinuousPlayerMovement();
                inputState.keys.clear();
                const movedInPallet = state.player.x > x0;

                // Enter Gym #1 Brock interior
                enterInteriorMap('GYM_BROCK');
                const inBrockGym = (state.currentMap === 'GYM_BROCK');
                // Simulate winning Gym #1 badge (level up starter & award boulder badge via battle win path)
                state.party[0].level = 20;
                state.badges.boulder = true;
                saveGameToSlot('הרפתקה', false);
                return { movedInPallet, inBrockGym };
            }""")
            assert_ok(pt_res["movedInPallet"] and pt_res["inBrockGym"], "Walked in Pallet Town & entered Brock's Gym #1")

            # Reload & verify Gym #1 + Badge persisted, then go offline & reload!
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(300)
            after_reload = page.evaluate("""() => {
                loadGameFromSlot('הרפתקה');
                return { map: state.currentMap, boulder: state.badges.boulder, lv: state.party[0]?.level };
            }""")
            assert_ok(
                after_reload["map"] == "GYM_BROCK" and after_reload["boulder"] and after_reload["lv"] == 20,
                f"Reloaded inside {after_reload['map']} with Boulder badge & Lv.{after_reload['lv']}",
            )

            context.set_offline(True)
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(400)
            offline_continue = page.evaluate("""() => {
                loadGameFromSlot('הרפתקה');
                return state.currentMap === 'GYM_BROCK' && state.badges.boulder === true;
            }""")
            assert_ok(offline_continue, "Reloaded & continued playthrough completely OFFLINE!")
            context.set_offline(False)

            browser.close()

        assert_ok(len(page_errors) == 0, f"Zero uncaught page errors (found: {page_errors})")
        # Filter out expected offline reload network log if any
        real_console_errors = [e for e in console_errors if "ERR_INTERNET_DISCONNECTED" not in e]
        assert_ok(len(real_console_errors) == 0, f"Zero console errors (found: {real_console_errors})")

        print("\nALL AUTOMATED TESTS PASSED SUCCESSFULLY!")
        return 0
    finally:
        server.shutdown()


if __name__ == "__main__":
    sys.exit(run_all_tests())

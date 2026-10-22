# Session Example: Space Defender Game Project

This reference documents the concrete collaboration from the 2026-07-12 session where Hermes + OpenClaw(小蛋) built a Pygame game via file bridge.

## Bridge Path
```
C:\Users\Lenovo\.openclaw\workspace\考研英语\bridge\
```
Note: This bridge was originally set up for English exam prep but was reused for the game project.

## Task Decomposition

| Task | File | Status |
|------|------|--------|
| 1/3 | game_config.py | ✅ Written by 蛋, reviewed by Hermes |
| 2/3 | game_entities.py | ✅ Written by 蛋, reviewed by Hermes |
| 3/3 | main.py | ⏳ Pending |

## Architecture Overview

**Space Defender** — Pygame game with 3 modules:

```
E:/workspace/space_defender/
├── game_config.py     # Constants: screen, player, bullet, enemy, game rules, colors
├── game_entities.py   # Classes: Player, Bullet, Enemy, Star
├── main.py            # Game loop: init, events, update, collision, HUD, GAME OVER
└── run_game.py        # Entry point: from main import run; run()  [created by Hermes]
```

### Class Interfaces

**Player**: `__init__()` → `move_left()` → `move_right()` → `draw(screen)` → `get_rect()` → `get_shoot_position()`

**Bullet**: `__init__(x, y)` → `update()` → `draw(screen)` → `is_off_screen()`

**Enemy**: `__init__(speed)` → `update()` → `draw(screen)` → `is_off_screen()`

**Star**: `__init__()` → `update()` (sine-wave flicker + slow fall) → `draw(screen)`

### Game State dict
```python
game_state = {
    'score': 0,
    'lives': INITIAL_LIVES,
    'level': 1,
    'current_speed': ENEMY_BASE_SPEED,
    'frame_count': 0,
    'last_shot_frame': 0
}
```

## Protocol Files

### from-Hermes.md format
```
# 来自 Hermes — YYYY-MM-DD

[HERMES]

[HERMES_TASK TaskN/M]

...task description...

[CODE_TEMPLATE]
```python
# template with TODO markers
```
```

### from-蛋.md response format
```
# 来自蛋 — YYYY-MM-DD

[OPENCLAW] 任务完成

[CODE]
path/to/file.py 已创建，包含...
...

[TEST_SUGGESTION]
```python
# verification script
```
```

## Watcher Setup
Python background script polling every 10s, stored at `~/AppData/Local/hermes/scripts/bridge_watcher.py`.
Detects `status: "ready"`, resets to `"idle"`, prints alert.

## Lessons Learned

1. **"processing" != "ready"** — 蛋 used "processing" as completion status. Cron didn't catch it. Solution: explicitly tell the peer to use "ready".
2. **Watcher exits after one detection** — must be restarted after each task cycle.
3. **Path quirks** — `$APPDATA` in git-bash resolves to `Roaming` not `Local`. Use absolute paths with `/c/...` prefix.
4. **Background process race** — 蛋 completed Task2 before Hermes finished writing Task2 instructions. The 10s watcher caught it instantly.

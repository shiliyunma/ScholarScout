# Session Protocol: Hermes + OpenClaw Game Development

From session 2026-07-12. The user (大浩/Dahao) defined this protocol for Hermes to act as **architect/PM** and OpenClaw (小蛋/Little Egg) as **implementer** for a Pygame "Space Defender" project.

## Bridge Location

```
C:\Users\Lenovo\.openclaw\workspace\考研英语\bridge\
```

## Files

- `status.json` — handshake: "idle" | "waiting" | "ready" | "error"
- `from-Hermes.md` — Hermes writes tasks here
- `from-蛋.md` — OpenClaw writes responses here

## Protocol from User

```
[HERMES] prefix for all replies
[TASK] + [CODE_TEMPLATE] for tasking
One task at a time
Max 2 review-retry cycles per task
```

## Project Structure (Space Defender)

```
E:/workspace/space_defender/
├── game_config.py    # Constants: screen, player, enemy, bullet, game rules, colors
├── game_entities.py  # Classes: Player, Enemy, Bullet, Star
├── main.py           # Main loop, events, collision, game state
└── run_game.py       # Entry point: `from main import run; run()`
```

## Task Breakdown

| Task | File | Content |
|------|------|---------|
| 1 | game_config.py | All constants (screen size, speeds, colors, game rules) |
| 2 | game_entities.py | Player (triangle ship), Enemy (red falling square), Bullet (upward projectile), Star (twinkling background) |
| 3 | main.py | init_game, draw_background, handle_events, update, collision detection, show_start_screen (3s), show_game_over, run() |

## Cron Monitor

Set to every 2 minutes (`*/2 * * * *`), checks status.json for "ready", notifies Hermes.

## Current Task Status (as of session end)

| Field | Value |
|-------|-------|
| last_from_hermes | Task1 game_config.py |
| task_id | game-task1 |
| status | waiting |

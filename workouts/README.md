# Back on the bike: 6-week base plan

Thirty-minute sessions for returning after two years off, with a history of injuries.
The priority is to **build consistency without getting hurt**. Fitness follows.
Every session is mostly easy, and intensity rises slowly over six weeks.

## Schedule

Three rides a week, with a rest day between rides (e.g. Mon / Wed / Sat).

| Week | Ride 1 | Ride 2 | Ride 3 |
|---|---|---|---|
| 1 | 01 Easy Spin | 07 Recovery Spin | 01 Easy Spin |
| 2 | 01 Easy Spin | 03 Cadence Skills | 02 Endurance Blocks |
| 3 | 02 Endurance Blocks | 03 Cadence Skills | 04 Tempo Taster |
| 4 | 02 Endurance Blocks | 06 Rolling Road | 04 Tempo Taster |
| 5 | 08 Endurance Pyramid | 03 Cadence Skills | 05 Sweet Spot Starter |
| 6 | 02 Endurance Blocks | 06 Rolling Road | 05 Sweet Spot Starter |

Run a workout with:
```
python tacx_neo.py dashboard --workout workouts\base_01_easy_spin.workout.json --hrm
```

## Rules for moving up

- **Only move to the next week when the current one was pain-free**, both during the
  rides and the day after. Otherwise repeat the week, or swap a ride for 07 Recovery
  Spin. Repeating weeks is normal, not failure.
- **Stop at the first sign of joint pain.** Knees, hips and lower back are the usual
  places. Muscle tiredness is fine; sharp, pinching or one-sided pain isn't. The
  trainer means you're never far from home, so use that.
- **Easy means easy.** On 01, 02 and 07 you should be able to talk in full sentences.
  If you can't, lower the FTP spinner. 145 W is an estimate, so expect to adjust it.
  If week 1 feels hard, ride at 90%. If everything feels trivial by week 3, try
  105–110%.
- **Spin, don't grind.** Keep cadence at 85–95 rpm. Pushing big gears at low cadence
  loads the knees, and it's a common cause of the kind of injuries you've had. The
  cadence targets on the dashboard turn red when you drop out of range.
- **Bike fit matters more indoors.** You don't move around on a trainer as you do on
  the road, so a saddle a few millimetres too high or low shows up quickly as knee or
  hip pain. If old injuries flare, check the fit before adding training. With your
  injury history, a physio or doctor check before starting is worthwhile.

## The workouts

| File | What it's for | Intensity |
|---|---|---|
| `base_01_easy_spin` | Re-learning to sit on a bike for 30 minutes | 55% for 20 min |
| `base_02_endurance` | Aerobic base; stand-and-stretch breaks every 8 min | 62% blocks |
| `base_03_cadence_skills` | Smooth pedalling at 80 / 90 / 100 rpm, low power | 55% |
| `base_04_tempo_taster` | First steady efforts | 3 × 4 min at 75% |
| `base_05_sweet_spot_starter` | First "real" intervals | 2 × 7 min at 85% |
| `base_06_rolling_road` | Slope mode, 0–2%; practise using gears | depends on your gearing |
| `base_07_recovery` | Tired or slightly sore days | 50% |
| `base_08_endurance_pyramid` | Gradual build: 60 → 75 → 60% in 3-minute steps | up to 75% |

At FTP 145 W, 55% is about 80 W, 75% about 109 W and 85% about 123 W. Run `check` on any
file to see its exact watts:
```
python tacx_neo.py check workouts\base_04_tempo_taster.workout.json
```

## After week 6

If all six weeks were pain-free, you'll have a base to build on. Next steps would be
longer rides (45–60 min of endurance), or a proper FTP test so the percentages match
your real fitness.

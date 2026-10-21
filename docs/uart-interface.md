# MQ Gas Board UART Interface

The MQ gas board continuously broadcasts the latest reading from each of its six MQ gas
sensors on a transmit-only UART, as NMEA-style text sentences (the same framing GPS modules
use): one sentence per sensor, one line per sentence, repeated at a fixed interval. The
board only sends; it accepts no commands.

This is the part of the board's interface specification (`INTERFACE.md` in
[mq-board-micropython](https://github.com/Mescalero66/mq-board-micropython)) that the
station relies on. If the two disagree, the board's specification wins. For how the station
uses this interface, see [MQ Gas Board](sensors/mq-gas-board.md).

## Physical Layer

| Parameter    | Value                                    |
|--------------|------------------------------------------|
| Baud rate    | 115200                                   |
| Framing      | 8 data bits, no parity, 1 stop bit (8N1) |
| Flow control | none                                     |
| Encoding     | ASCII                                    |

## Broadcast

Every broadcast is six sentences sent back to back, always in this order:

    MQ2, MQ4, MQ5, MQ8, MQ135, MQ7

- **Timing.** After the sixth sentence the board waits the broadcast interval (default
  10 s), then sends all six again. A full broadcast is about 250–300 bytes, which takes
  about 25 ms on the line.
- **No markers.** There is no start- or end-of-broadcast marker; the gap between
  broadcasts is the only separator.
- **The interval is approximate.** Broadcasts drift slowly relative to any clock, so
  don't rely on precise timing.
- **Broadcasting starts at boot**, before any sensor has been read, so the first
  sentences have empty fields (see [Examples](#examples)).
- **Latest reading only.** Each sentence carries the latest reading for that sensor.
  Sensors are read one at a time, and a new reading replaces that sensor's sentence from
  the next broadcast onward. The other five sentences are unaffected. The board never
  waits for all six sensors before broadcasting.
- **No partial updates.** All six sentences in one broadcast come from the same instant;
  a reading is never replaced mid-broadcast.
- **Repeats.** The same reading is repeated in every broadcast until that sensor is read
  again, about 15 times at the default settings. The `uptime_s` field identifies a reading, so
  a receiver can tell a repeat from a new value.

Identify sentences by their sensor name field, not by their position in the broadcast.

## Sentence Format

    $PMQB,<name>,<uptime_s>,<gas>,<concentration_ppm>,<temperature_c>,<baseline_tracking>,<r0_kohm>*<CS><CR><LF>

| Part         | Description                                                               |
|--------------|---------------------------------------------------------------------------|
| `$`          | start of sentence                                                         |
| `PMQB`       | address: NMEA proprietary sentence (`P`) with maker code `MQB` (MQ board) |
| `,`          | field separator; every sentence has exactly 7 fields after the address    |
| `<name>` ... | data fields (see [Fields](#fields))                                       |
| `*`          | end of data; the checksum follows                                         |
| `<CS>`       | checksum, two uppercase hex digits                                        |
| `<CR><LF>`   | line terminator, `\r\n` (0x0D 0x0A)                                       |

Example:

    $PMQB,MQ4,86451,CH4,17.85,23.19,1,5.401877*2E

- No field ever contains a comma, `$` or `*`.
- A sentence is at most about 60 characters in normal operation, well inside NMEA's
  82-character limit. A 128-byte line buffer is ample; discard anything longer as corrupt.

## Checksum

The standard NMEA checksum: XOR every character **between** `$` and `*` (neither is
included), written as two uppercase hex digits.

Worked example, for `$PMQB,MQ2,26,SMK,12.97,22.44,0,0.606521*78`:

    body     = "PMQB,MQ2,26,SMK,12.97,22.44,0,0.606521"
    checksum = 'P' ^ 'M' ^ 'Q' ^ 'B' ^ ',' ^ 'M' ^ ... ^ '1'  =  0x78  ->  "78"

Discard any sentence whose checksum doesn't match; the same data is sent again in the next
broadcast.

## Fields

| # | Field               | Type    | Format      | Unit | Empty when                             |
|---|---------------------|---------|-------------|------|----------------------------------------|
| 1 | `name`              | string  | sensor name |      | never                                  |
| 2 | `uptime_s`          | integer | `%d`        | s    | sensor not yet read since boot         |
| 3 | `gas`               | string  | gas code    |      | never                                  |
| 4 | `concentration_ppm` | decimal | 2 decimals  | ppm  | not yet read, or no valid reading      |
| 5 | `temperature_c`     | decimal | 2 decimals  | °C   | not yet read, or no temperature sensor |
| 6 | `baseline_tracking` | integer | `0` or `1`  |      | sensor not yet read since boot         |
| 7 | `r0_kohm`           | decimal | 6 decimals  | kΩ   | sensor not yet read since boot         |

Field names end in their unit where they have one (`_s`, `_ppm`, `_c`, `_kohm`). Fields
are identified by position; their names aren't transmitted, so they appear only in this
specification and in receivers' code. (Earlier versions of the specification called them
`t_s`, `ppm`, `temp_c`, `tracking` and `r0`; the sentences themselves are unchanged.)

An empty field (two adjacent commas, or a comma before `*`) means "no value". Parse it as
null, never as zero.

All fields other than `name` and `gas` describe a single reading and were captured
together at the moment it was taken.

The station publishes these fields under the same names (see
[MQ Gas Board](sensors/mq-gas-board.md)).

### `name`

The sensor type: `MQ2`, `MQ4`, `MQ5`, `MQ7`, `MQ8` or `MQ135`. Each name appears once per
broadcast. Ignore (rather than fail on) a name you don't recognise.

### `uptime_s`

Board uptime, in whole seconds, when the reading was taken. It is the reading's identity:
the same `uptime_s` for the same sensor means the same reading repeated.

- It counts from 0 at every boot, so it **goes backwards when the board restarts**.
- It is not wall-clock time; the board has no clock (see
  [Receiver Requirements](#receiver-requirements)).
- It does not wrap.
- It advances by 150 per reading for each sensor (see [Update Timing](#update-timing)).
- Different sensors in the same broadcast have different `uptime_s` values, because they are
  read at different times.

### `gas`

The target gas the `concentration_ppm` figure is calibrated for. It is fixed for each sensor, and is
sent even before the sensor has been read:

| name    | gas   | Target gas              |
|---------|-------|-------------------------|
| `MQ2`   | `SMK` | smoke                   |
| `MQ4`   | `CH4` | methane                 |
| `MQ5`   | `LPG` | liquefied petroleum gas |
| `MQ7`   | `CO`  | carbon monoxide         |
| `MQ8`   | `H2`  | hydrogen                |
| `MQ135` | `NH3` | ammonia                 |

Codes are uppercase ASCII letters and digits (currently at most 3 characters, but don't
assume a maximum). A sensor's gas would only change if the board's firmware switched to a
different datasheet curve for it, so `name` and `gas` together can key a data series.

MQ sensors are cross-sensitive: each responds to several gases, and the ppm figure is
only accurate if the target gas is the one present. Treat readings as indicative.

### `concentration_ppm`

Concentration of the target gas, in parts per million, to 2 decimal places.

- **Empty** means no valid reading: either the read failed outright (e.g. an ADC
  communication error), or the sensor gave an unusable signal (e.g. no output from a
  disconnected sensor). The two aren't distinguished. `uptime_s` still advances, so this is a
  new reading that has no value.
- In clean air, readings sit near a sensor-specific floor, not zero (see
  [Typical Values](#typical-values)). This is a property of the datasheet curves, not
  real gas.
- The board computes with single-precision floats, so only about 7 significant digits
  are meaningful.
- In fault conditions (e.g. a sensor reading close to zero resistance) the value may be
  implausibly large. Also reject non-numeric values such as `inf` or `nan`.
- It is only meaningful as an absolute value while `baseline_tracking` is `1`.

### `temperature_c`

The ambient temperature (from the board's DS18X20 sensor) used to temperature-compensate
this reading, in °C, to 2 decimal places.

- **Empty** means the board has no working temperature sensor, so the ppm is
  uncompensated.
- The DS18X20 resolves 0.0625 °C, so values step in sixteenths (e.g. `22.44`, `22.50`).
- It is refreshed about every 50 s. If the temperature sensor starts failing after boot,
  the last good value keeps being reported.
- The compensation is only valid from 0 to 50 °C (see
  [Temperature Compensation](#temperature-compensation)).

### `baseline_tracking`

Whether the `r0_kohm` used for this reading was a tracked clean-air baseline (`1`) or a
provisional boot calibration (`0`).

- **`0`**: R0 came from a quick calibration at boot, which assumes the air was clean at
  that moment and was taken with the heaters still cold. The ppm values show changes,
  but not reliable absolute levels.
- **`1`**: R0 is an established baseline, either tracked on this run or restored from
  the board's flash memory at boot.

When it becomes `1`:

- A board with a saved baseline reports `1` from its first reading after boot.
- A board with no saved baseline (first boot, or new sensors) reports `0` for about
  26 hours: a 24-hour warm-up, then a 2-hour window to establish the baseline.
- The baseline is first saved to flash after 48 hours of uptime, then every 24 hours.
  A fresh board that restarts before its first save starts again at `0`.

`baseline_tracking` can change from `0` to `1` together with a step in `r0_kohm` and `concentration_ppm`; that is the
baseline replacing the boot calibration.

### `r0_kohm`

The sensor's clean-air reference resistance (R0) that this ppm was calculated with, in kΩ,
to 6 decimal places. The board adjusts R0 slowly over time to follow sensor ageing; it is
sent with each reading so receivers can see baseline changes, and can recompute ppm
against any other R0 (see [Recomputing ppm](#recomputing-ppm)).

- It changes at most once every 2 hours, and is rate-limited to rising at most 10% or
  falling at most 1% per day.
- **`10.000000` with `baseline_tracking` = `0`** almost certainly means the boot calibration
  failed and the sensor is running on a placeholder R0. Its ppm is not meaningful until
  tracking starts.

## Update Timing

Sensors are read in a fixed 150-second cycle, one sensor every 25 s:

| Offset (s) | 25  | 50  | 75  | 100 | 125   | 150 |
|------------|-----|-----|-----|-----|-------|-----|
| Sensor     | MQ2 | MQ4 | MQ5 | MQ8 | MQ135 | MQ7 |

- Each sensor gets a new reading (a new `uptime_s`) every 150 s. At a 10 s broadcast interval,
  a reading appears in about 15 consecutive broadcasts.
- About every 25 s, exactly one sentence in the broadcast changes.
- The cycle starts about 1 s after boot (once the first temperature is available), so
  the first readings arrive at roughly `uptime_s` = 26, 51, 76, 101, 126 and 151. Until then,
  each sentence has every field except `name` and `gas` empty.

## Typical Values

The ppm each sensor reports in clean air once `baseline_tracking` is `1`. These are the floors of
the datasheet curves, useful for sanity checks:

| name    | Clean-air ppm |
|---------|---------------|
| `MQ2`   | ~13           |
| `MQ4`   | ~16           |
| `MQ5`   | ~0.85         |
| `MQ7`   | ~0.65         |
| `MQ8`   | ~53           |
| `MQ135` | ~4.3          |

## Temperature Compensation

MQ sensor resistance drifts with ambient temperature, so the board uses its DS18X20
reading to normalise each sensor's resistance to the datasheet's 20 °C reference before
calculating ppm. Each sensor has its own correction curve, fitted from its datasheet over
0–50 °C.

- **Below 0 °C the correction is not valid**; the board is not designed to operate
  there.
- **No humidity sensor**, so 65 %RH is assumed. This costs little on most sensors, but
  matters on the MQ4: its humidity curves diverge at the hot end, giving about 20% error
  in ppm at 50 °C.
- The MQ8 and MQ135 corrections flatten above about 35 °C, matching their datasheets.
- The MQ135 is rated only to 45 °C; its correction at 50 °C is extrapolated.

## Recomputing ppm

Because each reading carries the R0 it used, ppm can be recomputed against any other R0
(for example a fixed reference R0, to compare readings from different days on the same
basis):

    concentration_ppm_new = concentration_ppm * (r0_kohm / r0_kohm_new) ^ b

where `b` is the sensor's curve exponent:

| name    | b      |
|---------|--------|
| `MQ2`   | -2.440 |
| `MQ4`   | -2.786 |
| `MQ5`   | -2.431 |
| `MQ7`   | -1.518 |
| `MQ8`   | -0.688 |
| `MQ135` | -2.473 |

For example, `$PMQB,MQ4,86451,CH4,17.85,23.19,1,5.401877*2E` recomputed against
R0 = 5.383251 gives `17.85 * (5.401877 / 5.383251) ^ -2.786 = 17.68` ppm. The result is
limited by the 2-decimal precision of the transmitted ppm.

## Examples

Line terminators (`\r\n`) are omitted below; every line ends in one. These sentences are
also used as test cases (`tests/test_mq_board.py` checks that every `$PMQB` sentence in
this document parses and has a valid checksum).

### Boot

First broadcast after power-up; nothing has been read yet:

    $PMQB,MQ2,,SMK,,,,*59
    $PMQB,MQ4,,CH4,,,,*35
    $PMQB,MQ5,,LPG,,,,*50
    $PMQB,MQ8,,H2,,,,*7C
    $PMQB,MQ135,,NH3,,,,*3C
    $PMQB,MQ7,,CO,,,,*05

About 30 s later, the MQ2 has been read once (on a boot calibration, so `baseline_tracking` is
`0`); the rest are still empty:

    $PMQB,MQ2,26,SMK,12.97,22.44,0,0.606521*78
    $PMQB,MQ4,,CH4,,,,*35
    $PMQB,MQ5,,LPG,,,,*50
    $PMQB,MQ8,,H2,,,,*7C
    $PMQB,MQ135,,NH3,,,,*3C
    $PMQB,MQ7,,CO,,,,*05

About 160 s after boot, every sensor has been read once:

    $PMQB,MQ2,26,SMK,12.97,22.44,0,0.606521*78
    $PMQB,MQ4,51,CH4,16.32,22.44,0,5.383251*12
    $PMQB,MQ5,76,LPG,0.85,22.44,0,4.912730*48
    $PMQB,MQ8,101,H2,52.54,22.50,0,1.274406*63
    $PMQB,MQ135,126,NH3,4.30,22.50,0,8.031977*16
    $PMQB,MQ7,151,CO,0.65,22.50,0,2.150834*21

The next broadcast (about 170 s) is identical. The one after that (about 180 s) has a new
MQ2 reading; only its line differs:

    $PMQB,MQ2,176,SMK,12.81,22.50,0,0.606521*4E
    $PMQB,MQ4,51,CH4,16.32,22.44,0,5.383251*12
    $PMQB,MQ5,76,LPG,0.85,22.44,0,4.912730*48
    $PMQB,MQ8,101,H2,52.54,22.50,0,1.274406*63
    $PMQB,MQ135,126,NH3,4.30,22.50,0,8.031977*16
    $PMQB,MQ7,151,CO,0.65,22.50,0,2.150834*21

### Steady State

A day into a run, on a board that restored its baselines at boot:

    $PMQB,MQ2,86426,SMK,14.02,23.19,1,0.611204*46
    $PMQB,MQ4,86451,CH4,17.85,23.19,1,5.401877*2E
    $PMQB,MQ5,86326,LPG,0.91,23.13,1,4.928110*7A
    $PMQB,MQ8,86351,H2,55.10,23.13,1,1.280533*66
    $PMQB,MQ135,86376,NH3,4.62,23.13,1,8.064209*1D
    $PMQB,MQ7,86401,CO,0.71,23.19,1,2.163390*22

The MQ2 and MQ4 have the newest readings (86426, 86451); the MQ5 has the oldest (86326)
and is next to be read.

### Faults

MQ4 read failed (e.g. ADC error) or gave no valid output (e.g. disconnected); new `uptime_s`,
empty ppm:

    $PMQB,MQ4,86601,CH4,,23.19,1,5.401877*0C

No temperature sensor fitted or detected; ppm is uncompensated:

    $PMQB,MQ2,175,SMK,12.97,,0,0.612840*6E

MQ4 boot calibration failed; placeholder R0, ppm not meaningful:

    $PMQB,MQ4,51,CH4,2.87,22.44,0,10.000000*13

### Restart

A board that restarts sends empty sentences again, then readings with small `uptime_s` values:

    $PMQB,MQ2,259226,SMK,14.10,23.44,1,0.611204*7B     before restart
    $PMQB,MQ2,,SMK,,,,*59                              after restart
    $PMQB,MQ2,26,SMK,14.08,23.50,1,0.611204*7B         first reading; uptime_s went backwards

This board had been running for 3 days, so its baselines were saved and `baseline_tracking` is `1`
straight after the restart.

## Receiver Requirements

What any receiver of this interface must do. The station implements all of it in
`src/measure_station/uart/mq_board.py`.

### Parsing

For each line received:

1. Discard bytes until a `$`, and read up to `\r\n`. Discard lines longer than 128 bytes.
2. Check the line has the form `$...*HH`, with exactly two hex digits after `*`.
3. Verify the checksum; discard the line on a mismatch.
4. Check the address is `PMQB`; ignore other addresses.
5. Split the body on `,`. Expect 8 parts (address + 7 fields), and discard the line if
   there are fewer. Ignore any extra trailing fields.
6. Convert each field, treating an empty string as null.

### Detecting New Readings

Keep the last `uptime_s` seen for each sensor name, and compare each valid sentence's `uptime_s`:

| `uptime_s` compared to last seen | Meaning                                        |
|----------------------------------|------------------------------------------------|
| empty                       | not yet read since boot                        |
| no previous value           | new reading (first seen)                       |
| same                        | repeat of a reading already processed; ignore  |
| greater                     | new reading                                    |
| less                        | the board restarted; new reading               |

A new `uptime_s` means a new read attempt, not necessarily good data: check that `concentration_ppm` is not
empty before using it.

### Detecting Stale Data

The board doesn't broadcast its current uptime, so use the receiver's own clock:

- If a sensor's `uptime_s` hasn't changed for more than about 300 s (two cycles) while
  broadcasts keep arriving, that sensor has stopped updating.
- If no sentences arrive for more than about 30 s, the board or link is down.

### Timestamps

To give readings a wall-clock time, note the receiver's time when each new reading is
first seen. That is at most one broadcast interval (~10 s) after the reading was taken.

For better accuracy, estimate the board's boot time as the smallest `receive_time - uptime_s`
seen across new readings since the last restart; a reading's time is then
`boot_time + uptime_s`. Reset the estimate when a restart is detected.

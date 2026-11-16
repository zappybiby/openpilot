# sunny-dev: selected changes from sunnypilot #2094

Base: sunnypilot dev `2026.11.15-5034`, commit
`551cb67760be88c01b0189b6ac7dd3a1e364b20a`.

Source: [sunnypilot #2094](https://github.com/sunnypilot/sunnypilot/pull/2094),
head `4515836d003ee710f91b06fda0e3896d6371cf35`, reviewed November 16, 2026.
This is a selective backport, not a merge of that entire source branch.

## Included

- **Startup safety race:** clear `ControlsReady`, `FirmwareQueryDone`,
  `ObdMultiplexingChanged`, and `ObdMultiplexingEnabled` on the offroad
  transition, as in [openpilot #39207](https://github.com/commaai/openpilot/pull/39207).
- **Calibration:** seed the camera warp before accumulating calibration,
  start recalibration with zero valid blocks, account for the seed in progress,
  and discard old calibration when switching between recognized vehicles.
- **Radar messages:** preserve valid radar messages carrying radar faults;
  CAN errors still invalidate the message. Existing selfdrived fault handling remains active.
- **Dashcam alerts:** respect the openpilot enable setting on unrecognized cars
  and clear an old unrecognized-car offroad alert when the car is recognized.
- **Athena and sunnylink:** validate RPC arguments and local-proxy destinations;
  use an interruptible wait when retrying uploads.
- **Comma 4 setup:** report HTTP failures other than the existing 409 case.
- **Wi-Fi:** recover from D-Bus startup exceptions and constrain hotspot
  passwords to 8–63 characters.

## Prebuilt compatibility

The base contains ARM64 binaries and a `prebuilt` marker. Its launcher does
not rebuild C++ code. Updating `params_keys.h` alone would therefore leave the
startup race unchanged. The manager also removes the four readiness/OBD keys
synchronously on the offroad transition using the existing params library.
The header changes preserve the upstream behavior for a later source build.

The AGNOS 20.6 upgrade, CPU-rendered UI, `raylib_nb`, messaging/schema changes,
native dependency updates, model changes, and other coupled parts of #2094
require a coordinated rebuild. They are deferred. The existing AGNOS 19.9
configuration, models, schemas, and ARM64 binaries remain together.

## Verification

Run the host regression suite from the repository root:

```sh
python -m unittest discover -s tools/backport_tests -v
```

The suite executes production Python logic with device services replaced by
test doubles. Calibration tests use the production orientation math and the
upstream numerical tests. Restart tests retain the old compiled key flags and
exercise both manager-first and pandad-first scheduling. RPC tests exercise
the real dispatcher and registrations. UI tests exercise the changed setup,
password-validation, and D-Bus recovery paths.

Additional checks cover Python syntax, whitespace, unchanged native binaries
and boot dependencies, and the live comma 4 installer request. Physical-device
boot and driving tests are separate from these host checks.

## Installation and rollback

In comma 4 custom software setup, enter `zappybiby/sunny-dev`.
It expands to `https://installer.comma.ai/zappybiby/sunny-dev` and installs
`zappybiby/openpilot`, branch `sunny-dev`.

The original snapshot is preserved as `sunny-dev-before-2094-20261116` in the
same repository.

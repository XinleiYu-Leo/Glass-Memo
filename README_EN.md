# RoboMemo — collaboration snapshot

The shared GitHub repository is [Glass-Memo](https://github.com/XinleiYu-Leo/Glass-Memo); the app and code retain the RoboMemo name. This repository is public. Keep real recordings, account state and model attachments out of it.

RoboMemo explores wearable object memory: record useful evidence with glasses and an iPhone, maintain object-location observations, and answer where an item was last seen. This repository assembles the capture app, local demo, analysis tools, training code, aggregate results, and a separate model handoff.

Start with the offline demo (Node.js ≥22):

```sh
cd apps/demo
node server.mjs
```

Open http://127.0.0.1:8787/. All included examples are fictional geometric scenes. No account or cloud call is required. Optional real-video analysis uses a separate experimental local bridge and the collaborator's own authorized account; availability and billing must be checked separately.

| Component | Entry |
|---|---|
| iOS app, SDK 1.0.1, local signing and device testing | [apps/ios/README.md](apps/ios/README.md) |
| Offline demo / optional analysis | [apps/demo/README.md](apps/demo/README.md) |
| Optional model connection | [services/bridge/README.md](services/bridge/README.md) |
| Local sensor audit and presentation export | [tools/multisensor/README.md](tools/multisensor/README.md) |
| Current FHO training pipeline | [training/fho/README.md](training/fho/README.md) |
| Separate weights, hashes and installation | [models/README.md](models/README.md) |
| Pretrained hand/object baseline | [baselines/hand_object/README.md](baselines/hand_object/README.md) |

The latest capture app records video, optional in-stream PCM audio, IMU and device-state changes. Empty-audio false alarms and the multisensor movie timeline have been corrected. IMU delivery can still pause for several seconds before buffered samples arrive. Real-time stability and sensor synchronization still need further validation.

Initial pick/place model training uses Ego4D human action annotations as weak labels, a frozen MobileNetV3-Small visual encoder, and a trained temporal classification head. Experiments compare frame counts, training-set sizes, and audio inputs. Next steps are validation on our own glasses recordings and integration of the learned selector with the object-finding demo. On-phone inference and energy measurements remain unfinished. Detailed results are in the training directory and current-status document.

This is a sanitized collaboration snapshot, not a full experiment-data archive. Personal recordings, account state, device IDs, raw dataset assets, full labels and feature caches are excluded. Available saved project models are packaged separately; share them only as permitted by the applicable data and model terms. Missing experimental weights are documented rather than reconstructed. No real-dataset retraining or teacher inference was run during packaging; tests exercise synthetic fixtures and existing saved weights.

See the Chinese [project guide](README.md), [current status](docs/CURRENT_STATUS.md), [handoff](docs/TEAM_HANDOFF.md), [upload guide](docs/GITHUB_UPLOAD.md), [inventory](docs/INVENTORY.md), and [validation record](VALIDATION.json). Preserve component licenses; this repository does not grant a blanket open-source license. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

import csv
import json
import tempfile
import unittest
import wave
from pathlib import Path

from audit_session import audit, timestamp


def fixture(root, audio=True):
    root.mkdir(exist_ok=True)
    (root / "video.mov").write_bytes(b"not a movie; stdlib tests do not decode")
    (root / "session.json").write_text(json.dumps({"configuration": {
        "requestedResolution": "high", "requestedVideoFPS": 7,
        "requestedAudioEnabled": audio, "requestedMotionRateHz": 30}}))
    def write(name, values, fields=None):
        with (root / name).open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields or list(values[0]))
            writer.writeheader()
            writer.writerows(values)
    def ts(prefix, value, scale=1000):
        return {prefix+"Value": value, prefix+"Timescale": scale, prefix+"Epoch": 0, prefix+"Flags": 1}
    write("frames.csv", [{"result": "written", "sourceWidth": 720, "sourceHeight": 1280,
          **ts("sourcePTS", 10000+i*143), **ts("outputPTS",i*143), **ts("hostTime", 12000+i*143)} for i in range(7)])
    chunks = [{"result":"written", "inputFrames": 1024, "sampleRate":16000, "channels":1,
        "wavSampleOffset":i*1024, "wavByteOffset":44+i*2048,
        **ts("sourcePTS", 10000+i*64), **ts("hostTime",12000+i*64)} for i in range(16)]
    write("audio_chunks.csv", chunks if audio else [], list(chunks[0]))
    with wave.open(str(root / "audio.wav"),"wb") as w:
        w.setparams((1,2,16000,0,"NONE","not compressed"))
        w.writeframes(b"\0\0" * (16384 if audio else 0))
    write("motion.csv", [{"source":"fixture", "timestampNs":i*33333333, "requestedRateHz":30,
        **ts("hostTime",12000+i*34)} for i in range(30)])
    write("device_state.csv", [], ["changeIndex","sdkTimestamp"])


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        fixture(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_audio_and_relative_video(self):
        result = audit(self.root)
        self.assertTrue(result["all_structural_checks_passed"])
        self.assertEqual(result["audio"]["possible_source_gap_count"], 0)
        self.assertNotIn(str(self.root),json.dumps(result))

    def test_disabled_audio_is_valid_not_failure(self):
        fixture(self.root, audio=False)
        result = audit(self.root)
        self.assertTrue(result["all_structural_checks_passed"])
        self.assertEqual(result["audio"]["samples"],0)

    def test_wrong_movie_timeline_is_rejected(self):
        p=self.root / "frames.csv"
        p.write_text(p.read_text().replace("10000,1000,0,1,0,1000", "10000,1000,0,1,100,1000"))
        self.assertFalse(audit(self.root)["checks"]["video_output_matches_relative_source"])

    def test_source_gap_is_distinct_from_host_pause(self):
        p=self.root / "motion.csv"
        with p.open() as f:
            values=list(csv.DictReader(f))
        for i,r in enumerate(values):
            # Device time stays regular; batches arrive late at the phone.
            if i >= 15:
                r["hostTimeValue"]=str(int(r["hostTimeValue"])+8000)
        with p.open("w",newline="") as f:
            w=csv.DictWriter(f,fieldnames=list(values[0]));w.writeheader();w.writerows(values)
        result=audit(self.root)["motion"]["sources"][0]
        self.assertGreater(result["host_intervals_seconds"]["max"],8)
        self.assertEqual(result["source_gaps_over_2_5_periods_seconds"],[])

    def test_truncated_audio_is_detected(self):
        p=self.root / "audio.wav"
        p.write_bytes(p.read_bytes()[:-64])
        self.assertFalse(audit(self.root)["checks"]["wav_payload_matches_header"])

    def test_nonfinite_coremedia_flags_and_invalid_scales_are_rejected(self):
        for flags, scale in [(0,1000),(5,1000),(9,1000),(17,1000),(1,0),(1,-1000)]:
            with self.subTest(flags=flags, scale=scale), self.assertRaises(ValueError):
                timestamp({"tValue":123,"tTimescale":scale,"tFlags":flags},"t")
        self.assertEqual(float(timestamp({"tValue":1,"tTimescale":10,"tFlags":3},"t")),.1)

    def test_shareable_report_does_not_copy_freeform_fields(self):
        p=self.root / "session.json"
        value=json.loads(p.read_text())
        secret="PRIVATE_TEST_METADATA_DO_NOT_COPY"
        value["configuration"]["requestedResolution"]=secret
        value["configuration"]["requestedVideoFPS"]={"comment":secret}
        value["queue"]={"peakPendingDataJobs":secret,"maximumResidenceSeconds":3,
                        "overflowDroppedByModule":{secret:5,"motion":3,"deviceState":2}}
        value["device"]=secret
        p.write_text(json.dumps(value))
        result=audit(self.root)
        self.assertNotIn(secret,json.dumps(result))
        self.assertEqual(result["app_queue"]["overflowDroppedByModule"]["deviceState"],2)


if __name__ == "__main__":
    unittest.main()

"""Synthetic-only media regression checks. Nothing here loads private recordings."""
import csv
import json
import tempfile
import unittest
import wave
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

try:
    import av
    import numpy as np
    from export_av import export
except ModuleNotFoundError:
    av = None


def write_csv(path, records, fields=None):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields or list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def ts(prefix, value):
    value = Fraction(value)
    return {prefix + "Value": value.numerator, prefix + "Timescale": value.denominator,
            prefix + "Epoch": 0, prefix + "Flags": 1}


def synthetic_session(root):
    """Four seconds of geometric color, and tone from +0.3 to +3.8 seconds."""
    root.mkdir()
    with av.open(str(root / "video.mov"), "w") as container:
        container.metadata["comment"] = "PRIVATE_TEST_METADATA_DO_NOT_COPY"
        stream = container.add_stream("libx264", rate=7)
        stream.width, stream.height, stream.pix_fmt = 160, 96, "yuv420p"
        stream.codec_context.max_b_frames = 0
        stream.metadata["handler_name"] = "PRIVATE_TEST_METADATA_DO_NOT_COPY"
        for i in range(28):
            image = np.zeros((96,160,3), dtype=np.uint8)
            image[..., i % 3] = 180
            frame = av.VideoFrame.from_ndarray(image, format="rgb24")
            frame.pts, frame.time_base = i, Fraction(1,7)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    frames = [{"result":"written", "sourceWidth":160, "sourceHeight":96,
               **ts("sourcePTS", 10+Fraction(i,7)), **ts("outputPTS",Fraction(i,7)),
               **ts("hostTime", 12+Fraction(i,7))} for i in range(28)]
    write_csv(root / "frames.csv", frames)
    rate, block, blocks = 16000, 1000, 56
    chunks = [{"result":"written", "inputFrames":block, "sampleRate":rate,"channels":1,
               "wavSampleOffset":i*block,"wavByteOffset":44+i*block*2,
               **ts("sourcePTS",Fraction(103,10)+Fraction(i*block,rate)),
               **ts("hostTime",Fraction(123,10)+Fraction(i*block,rate))} for i in range(blocks)]
    write_csv(root / "audio_chunks.csv", chunks)
    pcm = (np.sin(2*np.pi*440*np.arange(block*blocks)/rate)*9000).astype("<i2")
    with wave.open(str(root / "audio.wav"), "wb") as w:
        w.setparams((1,2,rate,0,"NONE","not compressed")); w.writeframes(pcm.tobytes())
    motion = [{"source":"synthetic", "timestampNs":i*33333333,"requestedRateHz":30,
               **ts("hostTime",12+Fraction(i,30))} for i in range(120)]
    write_csv(root / "motion.csv", motion)
    write_csv(root / "device_state.csv", [], ["changeIndex","sdkTimestamp"])
    (root / "session.json").write_text(json.dumps({"configuration":{
        "requestedResolution":"high","requestedVideoFPS":7,"requestedAudioEnabled":True,
        "requestedMotionRateHz":30}, "privateDeviceId":"PRIVATE_TEST_METADATA_DO_NOT_COPY"}))


@unittest.skipIf(av is None, "PyAV/numpy are optional; install requirements.txt for AV checks")
class ExportAVTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.session = self.root / "input"
        synthetic_session(self.session)

    def tearDown(self):
        self.tmp.cleanup()

    def test_synthetic_av_preserves_video_pts_and_aligns_tone(self):
        result = export(self.session, self.root / "presentation.mp4")
        self.assertAlmostEqual(result["clock_slope"], 1, places=8)
        self.assertAlmostEqual(result["padded_seconds"], .5, delta=.001)
        self.assertFalse(result["capture_latency_calibrated"])
        with av.open(str(self.root / "presentation.mp4")) as out:
            self.assertEqual(len(out.streams.video),1)
            self.assertEqual(len(out.streams.audio),1)
            self.assertNotIn("PRIVATE_TEST_METADATA_DO_NOT_COPY", repr(out.metadata))
            self.assertNotIn("PRIVATE_TEST_METADATA_DO_NOT_COPY", repr([s.metadata for s in out.streams]))
            frames=list(out.decode(video=0))
            self.assertEqual(len(frames),28)
            self.assertEqual([Fraction(f.pts)*f.time_base for f in frames],[Fraction(i,7) for i in range(28)])
        with av.open(str(self.root / "presentation.mp4")) as out:
            # Decoder honors MP4's AAC priming edit; allow codec boundary spread.
            audio=np.concatenate([f.to_ndarray().reshape(-1) for f in out.decode(audio=0)])
            self.assertLess(np.sqrt(np.mean(audio[:3200]**2)),.002)
            self.assertGreater(np.sqrt(np.mean(audio[8000:16000]**2)),.1)
            self.assertLess(np.sqrt(np.mean(audio[62400:64000]**2)),.002)
        self.assertNotIn(str(self.session),json.dumps(result))
        self.assertFalse((self.root / "presentation.partial.mp4").exists())

    def test_existing_alignment_report_is_not_overwritten(self):
        report=self.root / "presentation.alignment.json"
        report.write_text("keep me")
        with self.assertRaises(ValueError):
            export(self.session,self.root / "presentation.mp4")
        self.assertEqual(report.read_text(),"keep me")
        self.assertFalse((self.root / "presentation.mp4").exists())

    def test_audio_gap_does_not_get_hidden_by_affine_export(self):
        p=self.session / "audio_chunks.csv"
        with p.open() as f:
            rows=list(csv.DictReader(f))
        for r in rows[25:]:
            source=Fraction(int(r["sourcePTSValue"]),int(r["sourcePTSTimescale"]))+Fraction(1,10)
            r.update(ts("sourcePTS",source))
        write_csv(p,rows)
        with self.assertRaisesRegex(ValueError,"source gaps"):
            export(self.session,self.root / "presentation.mp4")
        self.assertFalse((self.root / "presentation.mp4").exists())

    def test_partial_export_and_input_are_never_overwritten(self):
        partial=self.root / "presentation.partial.mp4"
        partial.write_bytes(b"keep me")
        with self.assertRaises(ValueError):
            export(self.session,self.root / "presentation.mp4")
        self.assertEqual(partial.read_bytes(),b"keep me")
        with self.assertRaises(ValueError):
            export(self.session,self.session / "presentation.mp4")

    def test_encoder_failure_cleans_only_its_own_output(self):
        before={p.name:p.read_bytes() for p in self.session.iterdir()}
        original_open=av.open
        def fail_writer(path,*args,**kwargs):
            if (args and args[0] == "w") or kwargs.get("mode") == "w":
                raise OSError("simulated encoder failure")
            return original_open(path,*args,**kwargs)
        with patch("export_av.av.open",side_effect=fail_writer), self.assertRaises(OSError):
            export(self.session,self.root / "presentation.mp4")
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.session.iterdir()})
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),["input"])

    def test_no_timeline_overlap_is_rejected_instead_of_silent_export(self):
        p=self.session / "audio_chunks.csv"
        with p.open() as f:
            rows=list(csv.DictReader(f))
        for r in rows:
            source=Fraction(int(r["sourcePTSValue"]),int(r["sourcePTSTimescale"]))+100
            r.update(ts("sourcePTS",source))
        write_csv(p,rows)
        with self.assertRaisesRegex(ValueError,"do not overlap"):
            export(self.session,self.root / "presentation.mp4")
        self.assertFalse((self.root / "presentation.mp4").exists())


if __name__ == "__main__":
    unittest.main()

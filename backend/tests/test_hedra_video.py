import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db import Base
from app.services import hedra_video_service as hedra, video_generation_service as video, job_service as jobs


class HedraTests(unittest.TestCase):
    def setUp(self):
        self.shot = {"shot_number": 1, "has_dialogue": True, "compiled_prompt": "A speaker.",
                     "dialogue_audio_url": "https://example.com/audio.wav", "still_frame_url": "https://example.com/still.jpg",
                     "characters_in_shot": ["Meera"], "speaker_label": "Meera", "duration_sec": 3}
        self.result = {"shots": [self.shot], "aspect_ratio": "16:9", "continuity": {"characters": [
            {"name": "Meera", "character_id": "vault", "image_url": "https://example.com/anime-variant.jpg"}]}}

    def test_style_variant_primary_and_nonvault_still(self):
        p = video.translate(self.result, self.shot)
        self.assertEqual(p["request"]["input"]["start_image"]["url"], "https://example.com/anime-variant.jpg")
        self.assertNotIn("duration_ms", p["request"]["input"])
        self.assertEqual(p["provider"], "hedra")
        del self.result["continuity"]["characters"][0]["character_id"]
        p = video.translate(self.result, self.shot)
        self.assertEqual(p["reference_source"], "module_o_still")
        self.assertEqual(p["request"]["input"]["start_image"]["url"], self.shot["still_frame_url"])

    def test_ambiguous_speaker_and_missing_image_stop_before_payment(self):
        self.shot["characters_in_shot"].append("Other")
        self.shot["speaker_label"] = "Unknown"
        self.result["continuity"]["characters"].append({"name": "Other"})
        with self.assertRaises(ValueError): hedra.preview(self.result, self.shot)
        self.shot["speaker_label"] = "Meera"
        self.result["continuity"]["characters"][0].pop("image_url")
        with self.assertRaises(ValueError): hedra.preview(self.result, self.shot)

    def test_input_fit_keeps_full_image(self):
        im = Image.new("RGB", (100, 200), "red");b = io.BytesIO();im.save(b, "PNG")
        raw, framing = hedra.fit_image(b.getvalue(), "16:9")
        out = Image.open(io.BytesIO(raw))
        self.assertEqual(out.size, (1280, 720));self.assertTrue(framing["matte_added"])
        self.assertGreater(out.getpixel((640, 5))[0], 200)
        self.assertGreater(out.getpixel((640, 710))[0], 200)

    def test_submission_dispatch_and_duplicate_safety(self):
        import wave
        audio = io.BytesIO()
        with wave.open(audio, "wb") as w:
            w.setparams((1,2,24000,0,"NONE","not compressed"));w.writeframes(b"\0\0" * 60000)
        im = io.BytesIO();Image.new("RGB", (320,180), "blue").save(im,"PNG")
        engine = create_engine("sqlite://");Base.metadata.create_all(engine)
        calls = []
        def api(method, path, **kwargs):
            calls.append((method,path,kwargs))
            return {"url":"https://example.com/upload"} if path == "/files" else {"job_id":"hedra-job"}
        with Session(engine) as db, patch.object(hedra.settings,"hedra_api_key","test"), patch.object(hedra,"api",side_effect=api), patch.object(hedra,"ffmpeg",return_value="ffmpeg"), patch.object(hedra,"download",side_effect=lambda url,limit: audio.getvalue() if "audio" in url else im.getvalue()), patch.object(video,"provider") as seedance:
            job = jobs.create_job(db,"test",ai_model="Seedance 2.0")
            jobs.set_result(db,job.id,self.result);jobs.set_status(db,job.id,"done")
            video.start(db,job.id,1)
            with self.assertRaises(ValueError):video.start(db,job.id,1)
            seedance.assert_not_called()
            self.assertEqual(len(calls),3)
            self.assertNotIn("duration_ms",calls[-1][2]["body"]["input"])
            saved = jobs.pending_videos(db)[0][1]
            self.assertEqual(saved["video_provider"],"hedra")
            self.assertEqual(saved["video_target_duration_sec"],2.5)
            with patch.object(hedra,"poll") as hp:
                video.poll(db,job.id,saved);hp.assert_called_once()
        engine.dispose()

    def test_real_ffmpeg_trim_checks_decoded_tracks(self):
        import subprocess
        with tempfile.TemporaryDirectory() as folder:
            source,target=Path(folder)/"source.mp4",Path(folder)/"trim.mp4"
            subprocess.run([hedra.ffmpeg(),"-nostdin","-loglevel","error","-y","-f","lavfi","-i","testsrc2=size=320x180:rate=25","-f","lavfi","-i","sine=frequency=440:sample_rate=24000","-t","3.24","-c:v","libx264","-c:a","aac",str(source)],check=True,timeout=60)
            evidence=hedra.trim(source,target,2.4746666667)
            for key in ["video_duration_sec","audio_duration_sec"]:
                self.assertLessEqual(abs(evidence["final"][key]-2.4746666667),0.05)
            with self.assertRaises(hedra.MediaValidationError):hedra.trim(source,target,5)


if __name__ == "__main__":unittest.main()

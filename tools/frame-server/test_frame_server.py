"""Testes das funcoes puras do frame_server. Rodar: python3 -m unittest test_frame_server."""
import tempfile
import unittest
from pathlib import Path

import frame_server


class ParseFrameQueryTest(unittest.TestCase):
    def test_defaults_give_full_frame(self) -> None:
        params = frame_server.parse_frame_query({"item": ["abc123"]})
        self.assertEqual((params["x"], params["y"], params["w"], params["h"]), (0, 0, 1, 1))
        self.assertEqual(params["out"], 1920)

    def test_rejects_non_alphanumeric_item(self) -> None:
        with self.assertRaises(frame_server.BadRequest) as caught:
            frame_server.parse_frame_query({"item": ["../etc/passwd"]})
        self.assertIn("../etc/passwd", str(caught.exception))

    def test_rejects_crop_outside_frame(self) -> None:
        with self.assertRaises(frame_server.BadRequest):
            frame_server.parse_frame_query({"item": ["a"], "x": ["0.8"], "w": ["0.5"]})

    def test_rejects_negative_time_and_tiny_crop(self) -> None:
        with self.assertRaises(frame_server.BadRequest):
            frame_server.parse_frame_query({"item": ["a"], "t": ["-1"]})
        with self.assertRaises(frame_server.BadRequest):
            frame_server.parse_frame_query({"item": ["a"], "w": ["0.001"]})

    def test_rejects_non_numeric_value_with_its_text(self) -> None:
        with self.assertRaises(frame_server.BadRequest) as caught:
            frame_server.parse_frame_query({"item": ["a"], "t": ["abc"]})
        self.assertIn("'abc'", str(caught.exception))


class MapMediaPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        (self.root / "Movies").mkdir()
        (self.root / "Movies" / "a.mp4").write_bytes(b"x")
        self.mappings = [("/media", str(self.root))]

    def tearDown(self) -> None:
        self.folder.cleanup()

    def test_maps_container_path_to_disk(self) -> None:
        mapped = frame_server.map_media_path("/media/Movies/a.mp4", self.mappings)
        self.assertEqual(mapped, (self.root / "Movies" / "a.mp4").resolve())

    def test_blocks_parent_traversal(self) -> None:
        with self.assertRaises(frame_server.BadRequest):
            frame_server.map_media_path("/media/../../etc/passwd", self.mappings)

    def test_blocks_path_outside_any_mapping(self) -> None:
        with self.assertRaises(frame_server.BadRequest) as caught:
            frame_server.map_media_path("/etc/passwd", self.mappings)
        self.assertIn("/etc/passwd", str(caught.exception))


class BuildFfmpegArgsTest(unittest.TestCase):
    def test_seek_crop_and_scale_are_in_the_command(self) -> None:
        params = {"t": 12.5, "x": 0.25, "y": 0.5, "w": 0.5, "h": 0.5, "out": 1280}
        args = frame_server.build_ffmpeg_args(Path("/tmp/a.mp4"), params)
        self.assertEqual(args[args.index("-ss") + 1], "12.500")
        filter_graph = args[args.index("-vf") + 1]
        self.assertIn("crop=iw*0.5:ih*0.5:iw*0.25:ih*0.5", filter_graph)
        self.assertIn("scale=1280:-2", filter_graph)

    def test_untagged_source_is_read_as_bt709_limited_range(self) -> None:
        params = {"t": 1, "x": 0, "y": 0, "w": 1, "h": 1, "out": 1920}
        graph = frame_server.build_ffmpeg_args(Path("/tmp/a.mp4"), params)
        graph = graph[graph.index("-vf") + 1]
        self.assertIn("in_color_matrix=bt709", graph)
        self.assertIn("in_range=tv", graph)
        self.assertIn("out_range=pc", graph)

    def test_eq_filter_is_appended_after_scale(self) -> None:
        params = {"t": 1, "x": 0, "y": 0, "w": 1, "h": 1, "out": 1920}
        args = frame_server.build_ffmpeg_args(Path("/tmp/a.mp4"), params, "eq=contrast=1.1")
        graph = args[args.index("-vf") + 1]
        self.assertTrue(graph.endswith(",eq=contrast=1.1"))

    def test_seek_comes_before_input_for_fast_seek(self) -> None:
        args = frame_server.build_ffmpeg_args(Path("/tmp/a.mp4"), {"t": 1, "x": 0, "y": 0, "w": 1, "h": 1, "out": 1920})
        self.assertLess(args.index("-ss"), args.index("-i"))


class BuildClipArgsTest(unittest.TestCase):
    PARAMS = {"t": 12.5, "x": 0.25, "y": 0.5, "w": 0.5, "h": 0.5, "out": 3840}

    def test_clip_is_h264_faststart_with_looped_single_frame(self) -> None:
        args = frame_server.build_clip_args(Path("/tmp/a.mp4"), self.PARAMS, Path("/tmp/o.mp4"))
        self.assertEqual(args[args.index("-c:v") + 1], "libx264")
        self.assertEqual(args[args.index("-movflags") + 1], "+faststart")
        self.assertEqual(args[-1], "/tmp/o.mp4")
        graph = args[args.index("-vf") + 1]
        self.assertIn("crop=iw*0.5:ih*0.5:iw*0.25:ih*0.5", graph)
        self.assertIn(f"loop=loop={frame_server.CLIP_FRAMES}:size=1", graph)

    def test_clip_does_not_convert_colour_so_tv_reads_it_like_the_movie(self) -> None:
        args = frame_server.build_clip_args(Path("/tmp/a.mp4"), self.PARAMS, Path("/tmp/o.mp4"))
        graph = args[args.index("-vf") + 1]
        self.assertNotIn("in_color_matrix", graph)
        self.assertNotIn("yuvj", graph)


class HdrDetectionTest(unittest.TestCase):
    def test_pq_and_hlg_are_hdr(self) -> None:
        self.assertTrue(frame_server.is_hdr_transfer("smpte2084"))
        self.assertTrue(frame_server.is_hdr_transfer("arib-std-b67"))

    def test_sdr_or_unknown_is_not_hdr(self) -> None:
        for transfer in ("bt709", "unknown", ""):
            self.assertFalse(frame_server.is_hdr_transfer(transfer))


class BuildHdrClipArgsTest(unittest.TestCase):
    PARAMS = {"t": 1.0, "x": 0, "y": 0, "w": 1, "h": 1, "out": 3840}

    def test_hdr_clip_is_hevc_10bit_tagged_hvc1(self) -> None:
        args = frame_server.build_clip_args(Path("/a.mp4"), self.PARAMS, Path("/o.mp4"), "smpte2084", "bt2020")
        self.assertEqual(args[args.index("-c:v") + 1], "libx265")
        self.assertEqual(args[args.index("-tag:v") + 1], "hvc1")
        self.assertIn("format=yuv420p10le", args[args.index("-vf") + 1])

    def test_hdr_clip_carries_the_source_colour_tags(self) -> None:
        args = frame_server.build_clip_args(Path("/a.mp4"), self.PARAMS, Path("/o.mp4"), "smpte2084", "bt2020")
        self.assertEqual(args[args.index("-color_trc") + 1], "smpte2084")
        self.assertEqual(args[args.index("-color_primaries") + 1], "bt2020")
        self.assertEqual(args[args.index("-colorspace") + 1], "bt2020nc")

    def test_hlg_clip_keeps_hlg_transfer(self) -> None:
        args = frame_server.build_clip_args(Path("/a.mp4"), self.PARAMS, Path("/o.mp4"), "arib-std-b67", "bt2020")
        self.assertEqual(args[args.index("-color_trc") + 1], "arib-std-b67")

    def test_sdr_clip_stays_h264_8bit(self) -> None:
        args = frame_server.build_clip_args(Path("/a.mp4"), self.PARAMS, Path("/o.mp4"), "bt709", "bt709")
        self.assertEqual(args[args.index("-c:v") + 1], "libx264")
        self.assertIn("format=yuv420p,", args[args.index("-vf") + 1])
        self.assertNotIn("-color_trc", args)


class ParseRangeTest(unittest.TestCase):
    def test_full_and_open_ended_ranges(self) -> None:
        self.assertEqual(frame_server.parse_range("bytes=0-99", 1000), (0, 99))
        self.assertEqual(frame_server.parse_range("bytes=900-", 1000), (900, 999))

    def test_absent_header_means_whole_body(self) -> None:
        self.assertIsNone(frame_server.parse_range(None, 1000))

    def test_end_is_clamped_to_size(self) -> None:
        self.assertEqual(frame_server.parse_range("bytes=0-5000", 1000), (0, 999))

    def test_rejects_malformed_or_out_of_range(self) -> None:
        with self.assertRaises(frame_server.BadRequest) as caught:
            frame_server.parse_range("bytes=abc", 1000)
        self.assertIn("bytes=abc", str(caught.exception))
        with self.assertRaises(frame_server.BadRequest):
            frame_server.parse_range("bytes=2000-", 1000)


class FrameCacheTest(unittest.TestCase):
    def test_evicts_oldest_beyond_capacity(self) -> None:
        cache = frame_server.FrameCache()
        for index in range(frame_server.CACHE_SIZE + 5):
            cache.put((index,), b"x")
        self.assertIsNone(cache.get((0,)))
        self.assertEqual(cache.get((frame_server.CACHE_SIZE + 4,)), b"x")


class ClipDurationTest(unittest.TestCase):
    def test_clip_is_a_quarter_second(self) -> None:
        self.assertAlmostEqual(frame_server.CLIP_FRAMES / frame_server.CLIP_FPS, 0.25, places=3)


class EffectiveOutTest(unittest.TestCase):
    HDR_SOURCE = {"width": 3840, "height": 2160, "transfer": "smpte2084", "primaries": "bt2020"}

    def _params(self, w: float) -> dict:
        return {"x": 0.0, "y": 0.0, "w": w, "h": w, "out": 3840}

    def test_full_frame_keeps_the_requested_output(self) -> None:
        self.assertEqual(frame_server.effective_out(self.HDR_SOURCE, self._params(1.0)), 3840)

    def test_four_times_zoom_stops_at_twice_the_native_crop(self) -> None:
        self.assertEqual(frame_server.effective_out(self.HDR_SOURCE, self._params(0.25)), 1920)

    def test_eight_times_zoom_keeps_a_floor_for_small_crops(self) -> None:
        self.assertEqual(frame_server.effective_out(self.HDR_SOURCE, self._params(0.125)), 960)

    def test_output_never_grows_above_the_request(self) -> None:
        fonte_pequena = {"width": 1280, "height": 720, "transfer": "bt709", "primaries": "bt709"}
        self.assertEqual(frame_server.effective_out(fonte_pequena, self._params(1.0)), 2560)

    def test_headers_report_the_effective_output(self) -> None:
        headers = frame_server.frame_headers(self.HDR_SOURCE, self._params(0.25))
        self.assertEqual(headers["X-Output-Px"], "1920x1080")


class ParseProbeOutputTest(unittest.TestCase):
    def test_reads_size_and_colour_tags(self) -> None:
        info = frame_server.parse_probe_output(
            "width=3840\nheight=2160\ncolor_transfer=smpte2084\ncolor_primaries=bt2020")
        self.assertEqual((info["width"], info["height"]), (3840, 2160))
        self.assertEqual(info["transfer"], "smpte2084")
        self.assertEqual(info["primaries"], "bt2020")

    def test_untagged_source_has_empty_colour_tags(self) -> None:
        info = frame_server.parse_probe_output("width=1920\nheight=800\n")
        self.assertEqual((info["width"], info["height"]), (1920, 800))
        self.assertEqual((info["transfer"], info["primaries"]), ("", ""))

    def test_missing_size_names_the_expected_format(self) -> None:
        with self.assertRaises(ValueError) as caught:
            frame_server.parse_probe_output("width=1920\n")
        self.assertIn("height", str(caught.exception))


class FrameHeadersTest(unittest.TestCase):
    HDR_SOURCE = {"width": 3840, "height": 2160, "transfer": "smpte2084", "primaries": "bt2020"}

    def test_full_frame_keeps_the_whole_source(self) -> None:
        params = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0, "out": 3840}
        headers = frame_server.frame_headers(self.HDR_SOURCE, params)
        self.assertEqual(headers["X-Source-Width"], "3840")
        self.assertEqual(headers["X-Crop-Source-Px"], "3840x2160")
        self.assertEqual(headers["X-Output-Px"], "3840x2160")

    def test_four_times_zoom_reports_the_native_crop(self) -> None:
        params = {"x": 0.375, "y": 0.375, "w": 0.25, "h": 0.25, "out": 3840}
        headers = frame_server.frame_headers(self.HDR_SOURCE, params)
        self.assertEqual(headers["X-Crop-Source-Px"], "960x540")

    def test_output_height_follows_the_crop_aspect_rounded_to_even(self) -> None:
        source = {"width": 1920, "height": 800, "transfer": "bt709", "primaries": "bt709"}
        headers = frame_server.frame_headers(source, {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0, "out": 1280})
        self.assertEqual(headers["X-Output-Px"], "1280x534")


class CheckClipOutputTest(unittest.TestCase):
    PARAMS = {"t": 29.9, "x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0, "out": 3840}

    def test_rejects_empty_clip_and_names_the_time(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            frame_server.check_clip_output(b"", self.PARAMS)
        self.assertIn("29.9", str(caught.exception))

    def test_accepts_a_real_clip(self) -> None:
        frame_server.check_clip_output(b"x" * 2000, self.PARAMS)


class DownloadGrantsTest(unittest.TestCase):
    PARAMS = {"t": 12.5, "x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5, "out": 3840}
    FILE = Path("/media/a.mp4")

    def test_grants_are_short_random_ids(self) -> None:
        grants = frame_server.DownloadGrants()
        grant_id = grants.add(self.PARAMS, self.FILE)
        self.assertEqual(len(grant_id), 8)
        self.assertRegex(grant_id, r"^[0-9a-f]+$")
        first, second = grants.add(self.PARAMS, self.FILE), grants.add(self.PARAMS, self.FILE)
        self.assertNotEqual(first, second)

    def test_returns_the_stored_params_until_expiry(self) -> None:
        grants = frame_server.DownloadGrants(ttl_seconds=60)
        grant_id = grants.add(self.PARAMS, self.FILE, now=1000.0)
        self.assertEqual(grants.get(grant_id, now=1059.0), (self.PARAMS, self.FILE))

    def test_expired_grant_is_forgotten(self) -> None:
        grants = frame_server.DownloadGrants(ttl_seconds=60)
        grant_id = grants.add(self.PARAMS, self.FILE, now=1000.0)
        self.assertIsNone(grants.get(grant_id, now=1061.0))
        self.assertIsNone(grants.get(grant_id, now=1001.0))

    def test_unknown_id_is_none(self) -> None:
        self.assertIsNone(frame_server.DownloadGrants().get("naoexiste"))


class BuildSaveArgsTest(unittest.TestCase):
    PARAMS = {"t": 12.5, "x": 0.25, "y": 0.25, "w": 0.25, "h": 0.25, "out": 3840}
    SDR = {"width": 3840, "height": 2160, "transfer": "bt709", "primaries": "bt709"}
    HDR = {"width": 3840, "height": 2160, "transfer": "smpte2084", "primaries": "bt2020"}

    def test_save_keeps_the_native_crop_width(self) -> None:
        args = frame_server.build_save_args(Path("/a.mp4"), self.PARAMS, self.SDR)
        graph = args[args.index("-vf") + 1]
        self.assertIn("crop=iw*0.25:ih*0.25:iw*0.25:ih*0.25", graph)
        self.assertIn("scale=960:-2", graph)
        self.assertIn("in_color_matrix=bt709", graph)

    def test_hdr_save_is_tonemapped_to_sdr(self) -> None:
        args = frame_server.build_save_args(Path("/a.mp4"), self.PARAMS, self.HDR)
        graph = args[args.index("-vf") + 1]
        self.assertIn("tonemap=hable", graph)
        self.assertIn("zscale=t=bt709:m=bt709:r=tv", graph)
        self.assertNotIn("in_color_matrix", graph)

    def test_save_is_a_single_jpeg_on_stdout(self) -> None:
        args = frame_server.build_save_args(Path("/a.mp4"), self.PARAMS, self.SDR)
        self.assertEqual(args[args.index("-frames:v") + 1], "1")
        self.assertEqual(args[-1], "pipe:1")
        self.assertEqual(args[args.index("-c:v") + 1], "mjpeg")


class SaveFilenameTest(unittest.TestCase):
    def test_name_carries_time_and_native_size(self) -> None:
        info = {"width": 3840, "height": 2160, "transfer": "bt709", "primaries": "bt709"}
        params = {"t": 340.533, "x": 0.0, "y": 0.0, "w": 0.25, "h": 0.25, "out": 3840}
        self.assertEqual(frame_server.save_filename(params, info), "quadro-340.5s-960x540.jpg")


class BuildQrArgsTest(unittest.TestCase):
    def test_qr_is_a_png_on_stdout(self) -> None:
        args = frame_server.build_qr_args("http://192.168.0.26:8790/save?id=abc")
        self.assertEqual(args[0], "qrencode")
        self.assertEqual(args[args.index("-t") + 1], "PNG")
        self.assertEqual(args[args.index("-o") + 1], "-")
        self.assertEqual(args[-1], "http://192.168.0.26:8790/save?id=abc")


if __name__ == "__main__":
    unittest.main()

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
        args = frame_server.build_clip_args(Path("/a.mp4"), self.PARAMS, Path("/o.mp4"), hdr=True)
        self.assertEqual(args[args.index("-c:v") + 1], "libx265")
        self.assertEqual(args[args.index("-tag:v") + 1], "hvc1")
        self.assertIn("format=yuv420p10le", args[args.index("-vf") + 1])

    def test_sdr_clip_stays_h264_8bit(self) -> None:
        args = frame_server.build_clip_args(Path("/a.mp4"), self.PARAMS, Path("/o.mp4"))
        self.assertEqual(args[args.index("-c:v") + 1], "libx264")
        self.assertIn("format=yuv420p,", args[args.index("-vf") + 1])


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


if __name__ == "__main__":
    unittest.main()

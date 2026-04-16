#!/usr/bin/env python3
"""
Shorts Auto Clipper
-------------------
Turns a source video into three 9:16 shorts with burned-in captions:

- the first clip
- the most interesting middle clip
- the last clip

Changes from the imported version:

- keeps the full source frame visible inside a phone-format layout
- speeds clip playback up to 1.5x
- keeps the reels-style captions and clickbait headline

USAGE:
    python3 shorts/karen_clipper.py myvideo.mp4

OUTPUT:
    ./shorts/clips/myvideo_clip_01.mp4
    ./shorts/clips/myvideo_clip_02.mp4
    ./shorts/clips/myvideo_clip_03.mp4
"""

import json
import os
import subprocess
import sys
import tempfile

# ── PATHS / OUTPUT ────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "clips")

# ── CLIP CONFIG ───────────────────────────────────────────────────────────────
CLIP_LENGTH = 55
MIN_CLIP = 40
MAX_CLIP = 62
PLAYBACK_SPEED = 1.5
WHISPER_MODEL = "base"

# ── VIDEO OUTPUT ──────────────────────────────────────────────────────────────
OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920
X264_PRESET = "medium"
X264_CRF = "18"
BACKGROUND_BLUR = "boxblur=20:10"
DEFAULT_YOUTUBE_TITLE_SUFFIX = " | Greentext Story"
DEFAULT_YOUTUBE_DESCRIPTION_PREFIX = "3chan-style greentext story short."
DEFAULT_YOUTUBE_HASHTAGS = ["#shorts", "#greentext", "#storytime"]
DEFAULT_YOUTUBE_TAGS = ["shorts", "greentext", "storytime", "imageboard story"]

# ── CAPTION STYLE ─────────────────────────────────────────────────────────────
CAPTION_FONT = "Noto Sans Black"
FONT_SIZE = 82
FONT_COLOR = "&H00FFFFFF"
HIGHLIGHT_COLOR = "&H0000D4FF"
OUTLINE_COLOR = "&H00000000"
OUTLINE_WIDTH = 7
SHADOW_DEPTH = 0
CAPTION_Y = 1360
MAX_CHARS_LINE = 18
PHRASE_SIZE = 3
CAPTION_FADE_MS = 80
HEADLINE_FONT_SIZE = 88
HEADLINE_MAX_CHARS = 16
HEADLINE_WORD_LIMIT = 7
HEADLINE_Y = 235
HEADLINE_BOX_COLOR = "&H20000000"
HEADLINE_SCALE_X = 108
HEADLINE_SCALE_Y = 112
HEADLINE_SPACING = 2

# ── MIDDLE CLIP HEURISTICS ────────────────────────────────────────────────────
INTEREST_KEYWORDS = {
    "actually",
    "caught",
    "crazy",
    "danger",
    "didn't",
    "doesn't",
    "everyone",
    "exposed",
    "finally",
    "imagine",
    "insane",
    "mistake",
    "never",
    "nobody",
    "problem",
    "really",
    "secret",
    "suddenly",
    "truth",
    "wait",
    "wrong",
}


def check_deps():
    """Make sure ffmpeg and whisper are installed."""
    missing = []
    result = subprocess.run(["ffmpeg", "-version"], capture_output=True)
    if result.returncode != 0:
        missing.append("ffmpeg  ->  brew install ffmpeg")

    try:
        import whisper  # noqa: F401
    except ImportError:
        missing.append("whisper  ->  pip install openai-whisper")

    if missing:
        detail = "\n".join(f"  {item}" for item in missing)
        raise RuntimeError(f"Missing dependencies:\n{detail}")


def get_video_duration(video_path):
    """Return video duration in seconds using ffprobe."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            video_path,
        ],
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def video_has_audio(video_path):
    """Return True when the source video contains at least one audio stream."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "json",
            video_path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    data = json.loads(result.stdout or "{}")
    return bool(data.get("streams"))


def transcribe_video(video_path):
    """Transcribe audio using Whisper, return word-level timestamps."""
    import whisper

    print("\nTranscribing audio...")
    model = whisper.load_model(WHISPER_MODEL)
    result = model.transcribe(
        video_path,
        word_timestamps=True,
        verbose=False,
        fp16=False,
    )

    words = []
    for seg in result["segments"]:
        for word in seg.get("words", []):
            words.append(
                {
                    "word": word["word"].strip(),
                    "start": word["start"],
                    "end": word["end"],
                }
            )

    print(f"Transcribed {len(words)} words")
    return words, result["segments"]


def find_clip_boundaries(segments, total_duration):
    """
    Split transcript into clip-sized chunks and prefer sentence boundaries.
    """
    sentence_ends = []
    for seg in segments:
        text = seg["text"].strip()
        if text and text[-1] in ".!?":
            sentence_ends.append(seg["end"])

    clips = []
    cursor = 0.0

    while cursor < total_duration - MIN_CLIP:
        target_end = cursor + CLIP_LENGTH
        window_ends = [
            timestamp
            for timestamp in sentence_ends
            if cursor + MIN_CLIP <= timestamp <= cursor + MAX_CLIP
        ]

        if window_ends:
            best_end = min(window_ends, key=lambda timestamp: abs(timestamp - target_end))
        else:
            best_end = min(cursor + MAX_CLIP, total_duration)

        clips.append((cursor, best_end))
        cursor = best_end

    if not clips and total_duration > 0:
        clips.append((0.0, min(total_duration, MAX_CLIP)))

    return clips


def words_in_range(words, start, end):
    """Return words fully contained in a time range."""
    return [word for word in words if word["start"] >= start and word["end"] <= end]


def segments_in_range(segments, start, end):
    """Return transcript segments that overlap a time range."""
    return [seg for seg in segments if seg["end"] > start and seg["start"] < end]


def wrap_text(text, max_chars):
    """Wrap text to roughly max_chars characters per line."""
    words = text.split()
    lines = []
    line = ""

    for word in words:
        proposed = (line + " " + word).strip()
        if len(proposed) <= max_chars:
            line = proposed
        else:
            if line:
                lines.append(line)
            line = word

    if line:
        lines.append(line)

    return "\n".join(lines)


def emphasize_reels_line(line):
    """Highlight the last word in a line for a louder reels-style look."""
    words = line.split()
    if not words:
        return ""
    words[-1] = f"{{\\c{HIGHLIGHT_COLOR}}}{words[-1]}{{\\c{FONT_COLOR}}}"
    return " ".join(words)


def build_reels_caption_text(text):
    """Format caption text as a bold, wrapped subtitle block."""
    wrapped_lines = wrap_text(text.upper(), MAX_CHARS_LINE).splitlines()
    styled_lines = [emphasize_reels_line(line) for line in wrapped_lines]
    return "\\N".join(styled_lines)


def build_clickbait_headline(segments, clip_start, clip_end):
    """Build a short headline from the opening line of the clip."""
    clip_segments = segments_in_range(segments, clip_start, clip_end)
    if not clip_segments:
        return None

    headline_words = []
    sentence_complete = False

    for seg in clip_segments:
        for word in seg["text"].strip().split():
            cleaned = word.strip()
            if not cleaned:
                continue
            headline_words.append(cleaned)
            if cleaned[-1] in ".!?":
                sentence_complete = True
                break
            if len(headline_words) >= HEADLINE_WORD_LIMIT:
                sentence_complete = True
                break
        if sentence_complete:
            break

    if not headline_words:
        return None

    headline = " ".join(headline_words).strip(" ,")
    headline = wrap_text(headline.upper(), HEADLINE_MAX_CHARS)
    return headline.replace("\n", "\\N")


def clean_headline_text(headline_text):
    """Convert ASS-style wrapped headline text into plain text."""
    return (headline_text or "").replace("\\N", " ").strip()


def build_youtube_title(segments, clip_start, clip_end):
    """Build a clickbait YouTube-ready title for the short."""
    headline_text = build_clickbait_headline(segments, clip_start, clip_end)
    cleaned = clean_headline_text(headline_text)
    base = cleaned or "YOU WON'T BELIEVE HOW THIS ENDS"
    title = f"{base}{DEFAULT_YOUTUBE_TITLE_SUFFIX}"
    return title[:100].rstrip()


def build_youtube_description(title):
    """Build the default YouTube description with hashtags."""
    hashtag_line = " ".join(DEFAULT_YOUTUBE_HASHTAGS)
    return f"{DEFAULT_YOUTUBE_DESCRIPTION_PREFIX}\n\n{title}\n\n{hashtag_line}"


def build_ass_subtitles(words, segments, clip_start, clip_end, playback_speed=1.0):
    """
    Build an ASS subtitle string for a clip.
    Subtitle timing is retimed to match the faster playback.
    """
    clip_words = words_in_range(words, clip_start, clip_end)
    if not clip_words:
        return None

    phrases = []
    for index in range(0, len(clip_words), PHRASE_SIZE):
        group = clip_words[index:index + PHRASE_SIZE]
        text = " ".join(word["word"] for word in group)
        start = (group[0]["start"] - clip_start) / playback_speed
        end = (group[-1]["end"] - clip_start) / playback_speed
        phrases.append((start, end, text))

    def fmt_time(seconds):
        total_cs = max(0, int(round(seconds * 100)))
        hours, rem = divmod(total_cs, 360000)
        minutes, rem = divmod(rem, 6000)
        secs, centis = divmod(rem, 100)
        return f"{hours}:{minutes:02}:{secs:02}.{centis:02}"

    ass = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {OUTPUT_WIDTH}\n"
        f"PlayResY: {OUTPUT_HEIGHT}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,"
        "OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,"
        "ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding\n"
        f"Style: Default,{CAPTION_FONT},{FONT_SIZE},{FONT_COLOR},"
        f"{HIGHLIGHT_COLOR},{OUTLINE_COLOR},&H64000000,"
        f"-1,0,0,0,100,100,0,0,1,{OUTLINE_WIDTH},{SHADOW_DEPTH},"
        f"5,60,60,0,1\n"
        f"Style: Headline,{CAPTION_FONT},{HEADLINE_FONT_SIZE},{FONT_COLOR},"
        f"{FONT_COLOR},&H00000000,{HEADLINE_BOX_COLOR},"
        f"-1,0,0,0,{HEADLINE_SCALE_X},{HEADLINE_SCALE_Y},{HEADLINE_SPACING},0,3,0,0,"
        f"8,80,80,80,1\n\n"
        "[Events]\n"
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )

    headline_text = build_clickbait_headline(segments, clip_start, clip_end)
    clip_runtime = (clip_end - clip_start) / playback_speed
    if headline_text:
        headline_tags = (
            "{"
            "\\an8"
            "\\b1"
            "\\shad0"
            f"\\fsp{HEADLINE_SPACING}"
            f"\\pos(540,{HEADLINE_Y})"
            f"\\fad({CAPTION_FADE_MS},{CAPTION_FADE_MS})"
            "}"
        )
        ass += (
            f"Dialogue: 1,0:00:00.00,{fmt_time(clip_runtime)},"
            f"Headline,,0,0,0,,{headline_tags}{headline_text}\n"
        )

    for start, end, text in phrases:
        end = min(end, clip_runtime)
        if end <= start:
            continue

        ass_text = build_reels_caption_text(text)
        reels_tags = (
            "{"
            "\\an5"
            "\\b1"
            "\\shad0"
            "\\fsp1"
            f"\\pos(540,{CAPTION_Y})"
            f"\\fad({CAPTION_FADE_MS},{CAPTION_FADE_MS})"
            "}"
        )
        ass += (
            f"Dialogue: 0,{fmt_time(start)},{fmt_time(end)},"
            f"Default,,0,0,0,,{reels_tags}{ass_text}\n"
        )

    return ass


def clip_interest_score(words, segments, start, end, total_duration):
    """Score a clip for mid-video selection using simple transcript heuristics."""
    clip_words = words_in_range(words, start, end)
    clip_segments = segments_in_range(segments, start, end)
    text = " ".join(seg["text"].strip() for seg in clip_segments).lower()
    duration = max(end - start, 1.0)

    keyword_hits = sum(text.count(keyword) for keyword in INTEREST_KEYWORDS)
    punctuation_hits = (text.count("?") * 2.5) + (text.count("!") * 2.0)
    number_hits = sum(1 for word in clip_words if any(char.isdigit() for char in word["word"]))
    density = len(clip_words) / duration
    midpoint = (start + end) / 2.0
    midpoint_ratio = midpoint / total_duration if total_duration > 0 else 0.5
    centrality = 1.0 - min(abs(midpoint_ratio - 0.5) / 0.5, 1.0)

    return (
        density * 3.0
        + keyword_hits * 1.8
        + punctuation_hits
        + number_hits * 0.35
        + len(clip_segments) * 0.25
        + centrality * 2.0
    )


def select_featured_clips(clip_ranges, words, segments, total_duration):
    """Return the first clip, best-scoring middle clip, and last clip."""
    if len(clip_ranges) <= 3:
        return clip_ranges

    middle_candidates = []
    for idx in range(1, len(clip_ranges) - 1):
        start, end = clip_ranges[idx]
        midpoint = (start + end) / 2.0
        midpoint_ratio = midpoint / total_duration if total_duration > 0 else 0.5
        score = clip_interest_score(words, segments, start, end, total_duration)
        middle_candidates.append((idx, start, end, midpoint_ratio, score))

    centered_candidates = [
        candidate
        for candidate in middle_candidates
        if 0.2 <= candidate[3] <= 0.8
    ]
    pool = centered_candidates or middle_candidates

    best_middle = max(
        pool,
        key=lambda candidate: (candidate[4], -abs(candidate[3] - 0.5)),
    )

    selected_indices = [0, best_middle[0], len(clip_ranges) - 1]
    return [clip_ranges[index] for index in selected_indices]


def export_clip(video_path, start, end, output_path, has_audio=True, ass_path=None):
    """
    Cut a clip, convert it to a full-frame 9:16 composition, speed it up,
    and optionally burn captions.
    """
    duration = end - start

    filter_parts = [
        (
            f"[0:v]scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:"
            f"force_original_aspect_ratio=increase,"
            f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},{BACKGROUND_BLUR}[bg]"
        ),
        (
            f"[0:v]scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:"
            f"force_original_aspect_ratio=decrease[fg]"
        ),
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[stacked]",
        f"[stacked]setpts=PTS/{PLAYBACK_SPEED},setsar=1[vbase]",
    ]

    video_label = "[vbase]"
    if ass_path:
        escaped = ass_path.replace("\\", "\\\\").replace(":", "\\:")
        filter_parts.append(f"[vbase]ass={escaped}[vout]")
        video_label = "[vout]"

    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        str(start),
        "-i",
        video_path,
        "-t",
        str(duration),
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        video_label,
        "-c:v",
        "libx264",
        "-preset",
        X264_PRESET,
        "-crf",
        X264_CRF,
        "-movflags",
        "+faststart",
    ]

    if has_audio:
        cmd.extend(
            [
                "-map",
                "0:a:0",
                "-filter:a",
                f"atempo={PLAYBACK_SPEED}",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
            ]
        )

    cmd.append(output_path)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    FFmpeg error: {result.stderr[-500:]}")
        return False
    return True


def generate_featured_shorts(video_path, output_dir=None, base_name_override=None, return_metadata=False):
    """
    Render three featured shorts and return their output paths.
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(video_path)

    check_deps()

    base_name = base_name_override or os.path.splitext(os.path.basename(video_path))[0]
    target_output_dir = output_dir or OUTPUT_DIR
    os.makedirs(target_output_dir, exist_ok=True)

    print("\nShorts Auto Clipper")
    print(f"  Input:  {video_path}")
    print(f"  Output: {target_output_dir}")
    print(f"  Speed:  {PLAYBACK_SPEED}x")

    duration = get_video_duration(video_path)
    has_audio = video_has_audio(video_path)
    print(f"  Duration: {duration / 60:.1f} minutes")

    words, segments = transcribe_video(video_path)
    clip_ranges = find_clip_boundaries(segments, duration)
    selected_ranges = select_featured_clips(clip_ranges, words, segments, duration)

    print(f"\nFound {len(clip_ranges)} possible clips")
    print(f"Exporting {len(selected_ranges)} featured clips")

    output_items = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for index, (start, end) in enumerate(selected_ranges, 1):
            clip_name = f"{base_name}_clip_{index:02d}.mp4"
            output_path = os.path.join(target_output_dir, clip_name)
            length = end - start
            speed_length = length / PLAYBACK_SPEED
            headline_text = build_clickbait_headline(segments, start, end)
            youtube_title = build_youtube_title(segments, start, end)
            youtube_description = build_youtube_description(youtube_title)

            print(f"\n  [{index}/{len(selected_ranges)}] {clip_name}")
            print(
                f"    source: {start / 60:.1f}min -> {end / 60:.1f}min  "
                f"({length:.0f}s -> {speed_length:.0f}s at {PLAYBACK_SPEED}x)"
            )

            ass_content = build_ass_subtitles(
                words,
                segments,
                start,
                end,
                playback_speed=PLAYBACK_SPEED,
            )
            ass_path = None
            if ass_content:
                ass_path = os.path.join(tmpdir, f"clip_{index:02d}.ass")
                with open(ass_path, "w", encoding="utf-8") as handle:
                    handle.write(ass_content)

            success = export_clip(
                video_path,
                start,
                end,
                output_path,
                has_audio=has_audio,
                ass_path=ass_path,
            )
            if not success:
                raise RuntimeError(f"failed to export clip {index}")

            size_mb = os.path.getsize(output_path) / 1024 / 1024
            print(f"    saved ({size_mb:.1f} MB)")
            output_items.append(
                {
                    "clip_index": index,
                    "output_path": output_path,
                    "source_start": start,
                    "source_end": end,
                    "headline": clean_headline_text(headline_text),
                    "youtube_title": youtube_title,
                    "youtube_description": youtube_description,
                    "youtube_hashtags": list(DEFAULT_YOUTUBE_HASHTAGS),
                    "youtube_tags": list(DEFAULT_YOUTUBE_TAGS),
                }
            )

    print(f"\nDone. Featured clips saved to {target_output_dir}\n")
    if return_metadata:
        return output_items
    return [item["output_path"] for item in output_items]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    video_path = sys.argv[1]
    if not os.path.exists(video_path):
        print(f"File not found: {video_path}")
        sys.exit(1)

    try:
        generate_featured_shorts(video_path, output_dir=OUTPUT_DIR)
    except Exception as exc:
        print(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()

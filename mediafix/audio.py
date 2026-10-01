import numpy as np

SAMPLING_RATE = 16000


class AudioError(Exception):
    pass


def _iter_frames(resampled):
    if resampled is None:
        return
    if isinstance(resampled, list):
        for frame in resampled:
            yield frame
        return
    try:
        for frame in resampled:
            yield frame
    except TypeError:
        yield resampled


def audio_stream_count(source) -> int:
    import av

    with av.open(source) as container:
        return len(container.streams.audio)


def extract_audio(source, audio_stream: int = 0, progress=None, cancel=None) -> np.ndarray:
    """Decode one audio stream to 16 kHz mono float32."""
    import av

    resampler = av.audio.resampler.AudioResampler(
        format="s16", layout="mono", rate=SAMPLING_RATE
    )
    chunks = []
    with av.open(source) as container:
        streams = container.streams.audio
        if not streams:
            raise AudioError("file has no audio stream")
        if audio_stream < 0 or audio_stream >= len(streams):
            raise AudioError(
                f"audio stream {audio_stream} out of range, file has {len(streams)} audio stream(s)"
            )
        stream = streams[audio_stream]
        total = float(stream.duration or 0) * float(stream.time_base or 1)
        for frame in container.decode(stream):
            if cancel is not None and cancel.is_set():
                raise AudioError("cancelled")
            for resampled in _iter_frames(resampler.resample(frame)):
                chunks.append(np.asarray(resampled.to_ndarray()).reshape(-1))
            if progress and total > 0:
                position = float(frame.pts or 0) * float(stream.time_base or 0)
                progress(min(0.99, position / total))
    for resampled in _iter_frames(resampler.resample(None)):
        chunks.append(np.asarray(resampled.to_ndarray()).reshape(-1))

    if not chunks:
        raise AudioError("no audio samples could be decoded")
    samples = np.concatenate(chunks).astype(np.float32) / 32768.0
    if samples.size == 0:
        raise AudioError("decoded audio is empty")
    return samples


def pick_audio_stream(info, preferred: int | None = None) -> int:
    """Choose which audio stream to transcribe."""
    if not info.audio:
        raise AudioError("file has no audio track")
    if preferred is not None:
        if 0 <= preferred < len(info.audio):
            return preferred
        raise AudioError(f"audio stream {preferred} out of range, file has {len(info.audio)}")
    for candidate in info.audio:
        if candidate.is_english and not candidate.is_surround and not candidate.voice:
            return candidate.index
    for candidate in info.audio:
        if candidate.default and not candidate.voice:
            return candidate.index
    for candidate in info.audio:
        if not candidate.voice:
            return candidate.index
    return info.audio[0].index


def iter_chunks(samples, chunk_seconds: int = 600, overlap_seconds: int = 2):
    """Yield (start_sample, end_sample, audio_slice) to bound peak memory."""
    total = int(samples.shape[0])
    span = chunk_seconds * SAMPLING_RATE
    overlap = overlap_seconds * SAMPLING_RATE
    start = 0
    while start < total:
        end = min(total, start + span)
        yield start, end, samples[start:end]
        if end >= total:
            break
        start = max(0, end - overlap)
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class AudioEncoding(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    AUDIO_ENCODING_UNSPECIFIED: _ClassVar[AudioEncoding]
    AUDIO_ENCODING_LINEAR16: _ClassVar[AudioEncoding]
    AUDIO_ENCODING_MULAW: _ClassVar[AudioEncoding]
    AUDIO_ENCODING_ALAW: _ClassVar[AudioEncoding]
    AUDIO_ENCODING_FLOAT32: _ClassVar[AudioEncoding]

class WarningCode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    WARNING_CODE_UNSPECIFIED: _ClassVar[WarningCode]
    WARNING_CODE_FALLING_BEHIND: _ClassVar[WarningCode]
    WARNING_CODE_AUDIO_DROPPED: _ClassVar[WarningCode]
    WARNING_CODE_MALFORMED_FRAME: _ClassVar[WarningCode]
    WARNING_CODE_NO_TURN_DETECTION: _ClassVar[WarningCode]
    WARNING_CODE_SERVER_DRAINING: _ClassVar[WarningCode]
    WARNING_CODE_WORKER_LOST: _ClassVar[WarningCode]
    WARNING_CODE_CALL_LIMIT_APPROACHING: _ClassVar[WarningCode]

class RecycleReason(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    RECYCLE_REASON_UNSPECIFIED: _ClassVar[RecycleReason]
    RECYCLE_REASON_RSS_THRESHOLD: _ClassVar[RecycleReason]
    RECYCLE_REASON_AUDIO_CAP: _ClassVar[RecycleReason]
    RECYCLE_REASON_EOU_OPPORTUNISTIC: _ClassVar[RecycleReason]
    RECYCLE_REASON_CRASH: _ClassVar[RecycleReason]
AUDIO_ENCODING_UNSPECIFIED: AudioEncoding
AUDIO_ENCODING_LINEAR16: AudioEncoding
AUDIO_ENCODING_MULAW: AudioEncoding
AUDIO_ENCODING_ALAW: AudioEncoding
AUDIO_ENCODING_FLOAT32: AudioEncoding
WARNING_CODE_UNSPECIFIED: WarningCode
WARNING_CODE_FALLING_BEHIND: WarningCode
WARNING_CODE_AUDIO_DROPPED: WarningCode
WARNING_CODE_MALFORMED_FRAME: WarningCode
WARNING_CODE_NO_TURN_DETECTION: WarningCode
WARNING_CODE_SERVER_DRAINING: WarningCode
WARNING_CODE_WORKER_LOST: WarningCode
WARNING_CODE_CALL_LIMIT_APPROACHING: WarningCode
RECYCLE_REASON_UNSPECIFIED: RecycleReason
RECYCLE_REASON_RSS_THRESHOLD: RecycleReason
RECYCLE_REASON_AUDIO_CAP: RecycleReason
RECYCLE_REASON_EOU_OPPORTUNISTIC: RecycleReason
RECYCLE_REASON_CRASH: RecycleReason

class StreamConfig(_message.Message):
    __slots__ = ("call_id", "encoding", "sample_rate_hz", "language", "model", "enable_word_timestamps")
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    ENCODING_FIELD_NUMBER: _ClassVar[int]
    SAMPLE_RATE_HZ_FIELD_NUMBER: _ClassVar[int]
    LANGUAGE_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    ENABLE_WORD_TIMESTAMPS_FIELD_NUMBER: _ClassVar[int]
    call_id: str
    encoding: AudioEncoding
    sample_rate_hz: int
    language: str
    model: str
    enable_word_timestamps: bool
    def __init__(self, call_id: _Optional[str] = ..., encoding: _Optional[_Union[AudioEncoding, str]] = ..., sample_rate_hz: _Optional[int] = ..., language: _Optional[str] = ..., model: _Optional[str] = ..., enable_word_timestamps: _Optional[bool] = ...) -> None: ...

class TranscriptionRequest(_message.Message):
    __slots__ = ("config", "audio")
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    AUDIO_FIELD_NUMBER: _ClassVar[int]
    config: StreamConfig
    audio: bytes
    def __init__(self, config: _Optional[_Union[StreamConfig, _Mapping]] = ..., audio: _Optional[bytes] = ...) -> None: ...

class Word(_message.Message):
    __slots__ = ("text", "start_sec", "end_sec", "confidence")
    TEXT_FIELD_NUMBER: _ClassVar[int]
    START_SEC_FIELD_NUMBER: _ClassVar[int]
    END_SEC_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    text: str
    start_sec: float
    end_sec: float
    confidence: float
    def __init__(self, text: _Optional[str] = ..., start_sec: _Optional[float] = ..., end_sec: _Optional[float] = ..., confidence: _Optional[float] = ...) -> None: ...

class TranscriptDelta(_message.Message):
    __slots__ = ("text", "words", "audio_offset_sec")
    TEXT_FIELD_NUMBER: _ClassVar[int]
    WORDS_FIELD_NUMBER: _ClassVar[int]
    AUDIO_OFFSET_SEC_FIELD_NUMBER: _ClassVar[int]
    text: str
    words: _containers.RepeatedCompositeFieldContainer[Word]
    audio_offset_sec: float
    def __init__(self, text: _Optional[str] = ..., words: _Optional[_Iterable[_Union[Word, _Mapping]]] = ..., audio_offset_sec: _Optional[float] = ...) -> None: ...

class EndOfUtterance(_message.Message):
    __slots__ = ("at_sec",)
    AT_SEC_FIELD_NUMBER: _ClassVar[int]
    at_sec: float
    def __init__(self, at_sec: _Optional[float] = ...) -> None: ...

class EndOfBoundary(_message.Message):
    __slots__ = ("at_sec",)
    AT_SEC_FIELD_NUMBER: _ClassVar[int]
    at_sec: float
    def __init__(self, at_sec: _Optional[float] = ...) -> None: ...

class Warning(_message.Message):
    __slots__ = ("code", "message", "at_sec", "behind_sec", "dropped_sec", "deadline_sec")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    AT_SEC_FIELD_NUMBER: _ClassVar[int]
    BEHIND_SEC_FIELD_NUMBER: _ClassVar[int]
    DROPPED_SEC_FIELD_NUMBER: _ClassVar[int]
    DEADLINE_SEC_FIELD_NUMBER: _ClassVar[int]
    code: WarningCode
    message: str
    at_sec: float
    behind_sec: float
    dropped_sec: float
    deadline_sec: float
    def __init__(self, code: _Optional[_Union[WarningCode, str]] = ..., message: _Optional[str] = ..., at_sec: _Optional[float] = ..., behind_sec: _Optional[float] = ..., dropped_sec: _Optional[float] = ..., deadline_sec: _Optional[float] = ...) -> None: ...

class Recycled(_message.Message):
    __slots__ = ("reason", "gap_sec", "at_audio_sec", "warm")
    REASON_FIELD_NUMBER: _ClassVar[int]
    GAP_SEC_FIELD_NUMBER: _ClassVar[int]
    AT_AUDIO_SEC_FIELD_NUMBER: _ClassVar[int]
    WARM_FIELD_NUMBER: _ClassVar[int]
    reason: RecycleReason
    gap_sec: float
    at_audio_sec: float
    warm: bool
    def __init__(self, reason: _Optional[_Union[RecycleReason, str]] = ..., gap_sec: _Optional[float] = ..., at_audio_sec: _Optional[float] = ..., warm: _Optional[bool] = ...) -> None: ...

class Ready(_message.Message):
    __slots__ = ("model", "supports_turn_detection", "has_punctuation", "model_chunk_ms", "accepted_sample_rate_hz")
    MODEL_FIELD_NUMBER: _ClassVar[int]
    SUPPORTS_TURN_DETECTION_FIELD_NUMBER: _ClassVar[int]
    HAS_PUNCTUATION_FIELD_NUMBER: _ClassVar[int]
    MODEL_CHUNK_MS_FIELD_NUMBER: _ClassVar[int]
    ACCEPTED_SAMPLE_RATE_HZ_FIELD_NUMBER: _ClassVar[int]
    model: str
    supports_turn_detection: bool
    has_punctuation: bool
    model_chunk_ms: int
    accepted_sample_rate_hz: int
    def __init__(self, model: _Optional[str] = ..., supports_turn_detection: _Optional[bool] = ..., has_punctuation: _Optional[bool] = ..., model_chunk_ms: _Optional[int] = ..., accepted_sample_rate_hz: _Optional[int] = ...) -> None: ...

class Final(_message.Message):
    __slots__ = ("text", "words", "total_audio_sec", "worker_generations")
    TEXT_FIELD_NUMBER: _ClassVar[int]
    WORDS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_AUDIO_SEC_FIELD_NUMBER: _ClassVar[int]
    WORKER_GENERATIONS_FIELD_NUMBER: _ClassVar[int]
    text: str
    words: _containers.RepeatedCompositeFieldContainer[Word]
    total_audio_sec: float
    worker_generations: int
    def __init__(self, text: _Optional[str] = ..., words: _Optional[_Iterable[_Union[Word, _Mapping]]] = ..., total_audio_sec: _Optional[float] = ..., worker_generations: _Optional[int] = ...) -> None: ...

class TranscriptionEvent(_message.Message):
    __slots__ = ("ready", "delta", "eou", "eob", "warning", "recycled", "final")
    READY_FIELD_NUMBER: _ClassVar[int]
    DELTA_FIELD_NUMBER: _ClassVar[int]
    EOU_FIELD_NUMBER: _ClassVar[int]
    EOB_FIELD_NUMBER: _ClassVar[int]
    WARNING_FIELD_NUMBER: _ClassVar[int]
    RECYCLED_FIELD_NUMBER: _ClassVar[int]
    FINAL_FIELD_NUMBER: _ClassVar[int]
    ready: Ready
    delta: TranscriptDelta
    eou: EndOfUtterance
    eob: EndOfBoundary
    warning: Warning
    recycled: Recycled
    final: Final
    def __init__(self, ready: _Optional[_Union[Ready, _Mapping]] = ..., delta: _Optional[_Union[TranscriptDelta, _Mapping]] = ..., eou: _Optional[_Union[EndOfUtterance, _Mapping]] = ..., eob: _Optional[_Union[EndOfBoundary, _Mapping]] = ..., warning: _Optional[_Union[Warning, _Mapping]] = ..., recycled: _Optional[_Union[Recycled, _Mapping]] = ..., final: _Optional[_Union[Final, _Mapping]] = ...) -> None: ...

class ServerInfoRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ServerInfoResponse(_message.Message):
    __slots__ = ("version", "built_at", "parakeet_ref", "parakeet_abi_version", "default_model", "backend", "ggml_cpu_features", "max_concurrent_calls", "active_calls", "warm_spares")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    BUILT_AT_FIELD_NUMBER: _ClassVar[int]
    PARAKEET_REF_FIELD_NUMBER: _ClassVar[int]
    PARAKEET_ABI_VERSION_FIELD_NUMBER: _ClassVar[int]
    DEFAULT_MODEL_FIELD_NUMBER: _ClassVar[int]
    BACKEND_FIELD_NUMBER: _ClassVar[int]
    GGML_CPU_FEATURES_FIELD_NUMBER: _ClassVar[int]
    MAX_CONCURRENT_CALLS_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_CALLS_FIELD_NUMBER: _ClassVar[int]
    WARM_SPARES_FIELD_NUMBER: _ClassVar[int]
    version: str
    built_at: str
    parakeet_ref: str
    parakeet_abi_version: int
    default_model: str
    backend: str
    ggml_cpu_features: str
    max_concurrent_calls: int
    active_calls: int
    warm_spares: int
    def __init__(self, version: _Optional[str] = ..., built_at: _Optional[str] = ..., parakeet_ref: _Optional[str] = ..., parakeet_abi_version: _Optional[int] = ..., default_model: _Optional[str] = ..., backend: _Optional[str] = ..., ggml_cpu_features: _Optional[str] = ..., max_concurrent_calls: _Optional[int] = ..., active_calls: _Optional[int] = ..., warm_spares: _Optional[int] = ...) -> None: ...

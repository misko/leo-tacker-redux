"""Pure, one-recording analysis implementations."""

from .api import (
    AnalysisConfigurationError,
    AnalysisExecutionContext,
    AnalysisInputError,
    QualityPsdAnalyzer,
    QualityPsdConfig,
    quality_psd_algorithm_ref,
    quality_psd_config_ref,
)
from .codec import (
    FEATURE_SET_FORMAT_ID,
    FEATURE_SET_MEDIA_TYPE,
    MAX_FEATURE_SET_BYTES,
    MalformedFeatureSetError,
    decode_feature_set,
    encode_feature_set,
)
from .decisions import MethodFiring, ThresholdRule, apply_threshold_rule
from .detector_suite import (
    DetectorSuiteConfig,
    IndependentDetectorSuite,
    detector_suite_algorithm_ref,
    detector_suite_config_ref,
)
from .persistence import (
    DurableFeatureSetRepository,
    FeatureSetIntegrityError,
    FeatureSetNotFoundError,
)

__all__ = [
    "FEATURE_SET_FORMAT_ID",
    "FEATURE_SET_MEDIA_TYPE",
    "MAX_FEATURE_SET_BYTES",
    "AnalysisConfigurationError",
    "AnalysisExecutionContext",
    "AnalysisInputError",
    "DetectorSuiteConfig",
    "DurableFeatureSetRepository",
    "FeatureSetIntegrityError",
    "FeatureSetNotFoundError",
    "IndependentDetectorSuite",
    "MalformedFeatureSetError",
    "MethodFiring",
    "QualityPsdAnalyzer",
    "QualityPsdConfig",
    "ThresholdRule",
    "apply_threshold_rule",
    "decode_feature_set",
    "detector_suite_algorithm_ref",
    "detector_suite_config_ref",
    "encode_feature_set",
    "quality_psd_algorithm_ref",
    "quality_psd_config_ref",
]

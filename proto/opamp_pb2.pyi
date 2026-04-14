import anyvalue_pb2 as _anyvalue_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class AgentToServerFlags(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    AgentToServerFlags_Unspecified: _ClassVar[AgentToServerFlags]
    AgentToServerFlags_RequestInstanceUid: _ClassVar[AgentToServerFlags]

class ServerToAgentFlags(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ServerToAgentFlags_Unspecified: _ClassVar[ServerToAgentFlags]
    ServerToAgentFlags_ReportFullState: _ClassVar[ServerToAgentFlags]
    ServerToAgentFlags_ReportAvailableComponents: _ClassVar[ServerToAgentFlags]

class ServerCapabilities(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ServerCapabilities_Unspecified: _ClassVar[ServerCapabilities]
    ServerCapabilities_AcceptsStatus: _ClassVar[ServerCapabilities]
    ServerCapabilities_OffersRemoteConfig: _ClassVar[ServerCapabilities]
    ServerCapabilities_AcceptsEffectiveConfig: _ClassVar[ServerCapabilities]
    ServerCapabilities_OffersPackages: _ClassVar[ServerCapabilities]
    ServerCapabilities_AcceptsPackagesStatus: _ClassVar[ServerCapabilities]
    ServerCapabilities_OffersConnectionSettings: _ClassVar[ServerCapabilities]
    ServerCapabilities_AcceptsConnectionSettingsRequest: _ClassVar[ServerCapabilities]

class PackageType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PackageType_TopLevel: _ClassVar[PackageType]
    PackageType_Addon: _ClassVar[PackageType]

class ServerErrorResponseType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ServerErrorResponseType_Unknown: _ClassVar[ServerErrorResponseType]
    ServerErrorResponseType_BadRequest: _ClassVar[ServerErrorResponseType]
    ServerErrorResponseType_Unavailable: _ClassVar[ServerErrorResponseType]

class CommandType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CommandType_Restart: _ClassVar[CommandType]

class AgentCapabilities(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    AgentCapabilities_Unspecified: _ClassVar[AgentCapabilities]
    AgentCapabilities_ReportsStatus: _ClassVar[AgentCapabilities]
    AgentCapabilities_AcceptsRemoteConfig: _ClassVar[AgentCapabilities]
    AgentCapabilities_ReportsEffectiveConfig: _ClassVar[AgentCapabilities]
    AgentCapabilities_AcceptsPackages: _ClassVar[AgentCapabilities]
    AgentCapabilities_ReportsPackageStatuses: _ClassVar[AgentCapabilities]
    AgentCapabilities_ReportsOwnTraces: _ClassVar[AgentCapabilities]
    AgentCapabilities_ReportsOwnMetrics: _ClassVar[AgentCapabilities]
    AgentCapabilities_ReportsOwnLogs: _ClassVar[AgentCapabilities]
    AgentCapabilities_AcceptsOpAMPConnectionSettings: _ClassVar[AgentCapabilities]
    AgentCapabilities_AcceptsOtherConnectionSettings: _ClassVar[AgentCapabilities]
    AgentCapabilities_AcceptsRestartCommand: _ClassVar[AgentCapabilities]
    AgentCapabilities_ReportsHealth: _ClassVar[AgentCapabilities]
    AgentCapabilities_ReportsRemoteConfig: _ClassVar[AgentCapabilities]
    AgentCapabilities_ReportsHeartbeat: _ClassVar[AgentCapabilities]
    AgentCapabilities_ReportsAvailableComponents: _ClassVar[AgentCapabilities]

class RemoteConfigStatuses(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    RemoteConfigStatuses_UNSET: _ClassVar[RemoteConfigStatuses]
    RemoteConfigStatuses_APPLIED: _ClassVar[RemoteConfigStatuses]
    RemoteConfigStatuses_APPLYING: _ClassVar[RemoteConfigStatuses]
    RemoteConfigStatuses_FAILED: _ClassVar[RemoteConfigStatuses]

class PackageStatusEnum(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PackageStatusEnum_Installed: _ClassVar[PackageStatusEnum]
    PackageStatusEnum_InstallPending: _ClassVar[PackageStatusEnum]
    PackageStatusEnum_Installing: _ClassVar[PackageStatusEnum]
    PackageStatusEnum_InstallFailed: _ClassVar[PackageStatusEnum]
    PackageStatusEnum_Downloading: _ClassVar[PackageStatusEnum]
AgentToServerFlags_Unspecified: AgentToServerFlags
AgentToServerFlags_RequestInstanceUid: AgentToServerFlags
ServerToAgentFlags_Unspecified: ServerToAgentFlags
ServerToAgentFlags_ReportFullState: ServerToAgentFlags
ServerToAgentFlags_ReportAvailableComponents: ServerToAgentFlags
ServerCapabilities_Unspecified: ServerCapabilities
ServerCapabilities_AcceptsStatus: ServerCapabilities
ServerCapabilities_OffersRemoteConfig: ServerCapabilities
ServerCapabilities_AcceptsEffectiveConfig: ServerCapabilities
ServerCapabilities_OffersPackages: ServerCapabilities
ServerCapabilities_AcceptsPackagesStatus: ServerCapabilities
ServerCapabilities_OffersConnectionSettings: ServerCapabilities
ServerCapabilities_AcceptsConnectionSettingsRequest: ServerCapabilities
PackageType_TopLevel: PackageType
PackageType_Addon: PackageType
ServerErrorResponseType_Unknown: ServerErrorResponseType
ServerErrorResponseType_BadRequest: ServerErrorResponseType
ServerErrorResponseType_Unavailable: ServerErrorResponseType
CommandType_Restart: CommandType
AgentCapabilities_Unspecified: AgentCapabilities
AgentCapabilities_ReportsStatus: AgentCapabilities
AgentCapabilities_AcceptsRemoteConfig: AgentCapabilities
AgentCapabilities_ReportsEffectiveConfig: AgentCapabilities
AgentCapabilities_AcceptsPackages: AgentCapabilities
AgentCapabilities_ReportsPackageStatuses: AgentCapabilities
AgentCapabilities_ReportsOwnTraces: AgentCapabilities
AgentCapabilities_ReportsOwnMetrics: AgentCapabilities
AgentCapabilities_ReportsOwnLogs: AgentCapabilities
AgentCapabilities_AcceptsOpAMPConnectionSettings: AgentCapabilities
AgentCapabilities_AcceptsOtherConnectionSettings: AgentCapabilities
AgentCapabilities_AcceptsRestartCommand: AgentCapabilities
AgentCapabilities_ReportsHealth: AgentCapabilities
AgentCapabilities_ReportsRemoteConfig: AgentCapabilities
AgentCapabilities_ReportsHeartbeat: AgentCapabilities
AgentCapabilities_ReportsAvailableComponents: AgentCapabilities
RemoteConfigStatuses_UNSET: RemoteConfigStatuses
RemoteConfigStatuses_APPLIED: RemoteConfigStatuses
RemoteConfigStatuses_APPLYING: RemoteConfigStatuses
RemoteConfigStatuses_FAILED: RemoteConfigStatuses
PackageStatusEnum_Installed: PackageStatusEnum
PackageStatusEnum_InstallPending: PackageStatusEnum
PackageStatusEnum_Installing: PackageStatusEnum
PackageStatusEnum_InstallFailed: PackageStatusEnum
PackageStatusEnum_Downloading: PackageStatusEnum

class AgentToServer(_message.Message):
    __slots__ = ("instance_uid", "sequence_num", "agent_description", "capabilities", "health", "effective_config", "remote_config_status", "package_statuses", "agent_disconnect", "flags", "connection_settings_request", "custom_capabilities", "custom_message", "available_components")
    INSTANCE_UID_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_NUM_FIELD_NUMBER: _ClassVar[int]
    AGENT_DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    HEALTH_FIELD_NUMBER: _ClassVar[int]
    EFFECTIVE_CONFIG_FIELD_NUMBER: _ClassVar[int]
    REMOTE_CONFIG_STATUS_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_STATUSES_FIELD_NUMBER: _ClassVar[int]
    AGENT_DISCONNECT_FIELD_NUMBER: _ClassVar[int]
    FLAGS_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_SETTINGS_REQUEST_FIELD_NUMBER: _ClassVar[int]
    CUSTOM_CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    CUSTOM_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_COMPONENTS_FIELD_NUMBER: _ClassVar[int]
    instance_uid: bytes
    sequence_num: int
    agent_description: AgentDescription
    capabilities: int
    health: ComponentHealth
    effective_config: EffectiveConfig
    remote_config_status: RemoteConfigStatus
    package_statuses: PackageStatuses
    agent_disconnect: AgentDisconnect
    flags: int
    connection_settings_request: ConnectionSettingsRequest
    custom_capabilities: CustomCapabilities
    custom_message: CustomMessage
    available_components: AvailableComponents
    def __init__(self, instance_uid: _Optional[bytes] = ..., sequence_num: _Optional[int] = ..., agent_description: _Optional[_Union[AgentDescription, _Mapping]] = ..., capabilities: _Optional[int] = ..., health: _Optional[_Union[ComponentHealth, _Mapping]] = ..., effective_config: _Optional[_Union[EffectiveConfig, _Mapping]] = ..., remote_config_status: _Optional[_Union[RemoteConfigStatus, _Mapping]] = ..., package_statuses: _Optional[_Union[PackageStatuses, _Mapping]] = ..., agent_disconnect: _Optional[_Union[AgentDisconnect, _Mapping]] = ..., flags: _Optional[int] = ..., connection_settings_request: _Optional[_Union[ConnectionSettingsRequest, _Mapping]] = ..., custom_capabilities: _Optional[_Union[CustomCapabilities, _Mapping]] = ..., custom_message: _Optional[_Union[CustomMessage, _Mapping]] = ..., available_components: _Optional[_Union[AvailableComponents, _Mapping]] = ...) -> None: ...

class AgentDisconnect(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ConnectionSettingsRequest(_message.Message):
    __slots__ = ("opamp",)
    OPAMP_FIELD_NUMBER: _ClassVar[int]
    opamp: OpAMPConnectionSettingsRequest
    def __init__(self, opamp: _Optional[_Union[OpAMPConnectionSettingsRequest, _Mapping]] = ...) -> None: ...

class OpAMPConnectionSettingsRequest(_message.Message):
    __slots__ = ("certificate_request",)
    CERTIFICATE_REQUEST_FIELD_NUMBER: _ClassVar[int]
    certificate_request: CertificateRequest
    def __init__(self, certificate_request: _Optional[_Union[CertificateRequest, _Mapping]] = ...) -> None: ...

class CertificateRequest(_message.Message):
    __slots__ = ("csr",)
    CSR_FIELD_NUMBER: _ClassVar[int]
    csr: bytes
    def __init__(self, csr: _Optional[bytes] = ...) -> None: ...

class AvailableComponents(_message.Message):
    __slots__ = ("components", "hash")
    class ComponentsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: ComponentDetails
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[ComponentDetails, _Mapping]] = ...) -> None: ...
    COMPONENTS_FIELD_NUMBER: _ClassVar[int]
    HASH_FIELD_NUMBER: _ClassVar[int]
    components: _containers.MessageMap[str, ComponentDetails]
    hash: bytes
    def __init__(self, components: _Optional[_Mapping[str, ComponentDetails]] = ..., hash: _Optional[bytes] = ...) -> None: ...

class ComponentDetails(_message.Message):
    __slots__ = ("metadata", "sub_component_map")
    class SubComponentMapEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: ComponentDetails
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[ComponentDetails, _Mapping]] = ...) -> None: ...
    METADATA_FIELD_NUMBER: _ClassVar[int]
    SUB_COMPONENT_MAP_FIELD_NUMBER: _ClassVar[int]
    metadata: _containers.RepeatedCompositeFieldContainer[_anyvalue_pb2.KeyValue]
    sub_component_map: _containers.MessageMap[str, ComponentDetails]
    def __init__(self, metadata: _Optional[_Iterable[_Union[_anyvalue_pb2.KeyValue, _Mapping]]] = ..., sub_component_map: _Optional[_Mapping[str, ComponentDetails]] = ...) -> None: ...

class ServerToAgent(_message.Message):
    __slots__ = ("instance_uid", "error_response", "remote_config", "connection_settings", "packages_available", "flags", "capabilities", "agent_identification", "command", "custom_capabilities", "custom_message")
    INSTANCE_UID_FIELD_NUMBER: _ClassVar[int]
    ERROR_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    REMOTE_CONFIG_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_SETTINGS_FIELD_NUMBER: _ClassVar[int]
    PACKAGES_AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    FLAGS_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    AGENT_IDENTIFICATION_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    CUSTOM_CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    CUSTOM_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    instance_uid: bytes
    error_response: ServerErrorResponse
    remote_config: AgentRemoteConfig
    connection_settings: ConnectionSettingsOffers
    packages_available: PackagesAvailable
    flags: int
    capabilities: int
    agent_identification: AgentIdentification
    command: ServerToAgentCommand
    custom_capabilities: CustomCapabilities
    custom_message: CustomMessage
    def __init__(self, instance_uid: _Optional[bytes] = ..., error_response: _Optional[_Union[ServerErrorResponse, _Mapping]] = ..., remote_config: _Optional[_Union[AgentRemoteConfig, _Mapping]] = ..., connection_settings: _Optional[_Union[ConnectionSettingsOffers, _Mapping]] = ..., packages_available: _Optional[_Union[PackagesAvailable, _Mapping]] = ..., flags: _Optional[int] = ..., capabilities: _Optional[int] = ..., agent_identification: _Optional[_Union[AgentIdentification, _Mapping]] = ..., command: _Optional[_Union[ServerToAgentCommand, _Mapping]] = ..., custom_capabilities: _Optional[_Union[CustomCapabilities, _Mapping]] = ..., custom_message: _Optional[_Union[CustomMessage, _Mapping]] = ...) -> None: ...

class OpAMPConnectionSettings(_message.Message):
    __slots__ = ("destination_endpoint", "headers", "certificate", "heartbeat_interval_seconds")
    DESTINATION_ENDPOINT_FIELD_NUMBER: _ClassVar[int]
    HEADERS_FIELD_NUMBER: _ClassVar[int]
    CERTIFICATE_FIELD_NUMBER: _ClassVar[int]
    HEARTBEAT_INTERVAL_SECONDS_FIELD_NUMBER: _ClassVar[int]
    destination_endpoint: str
    headers: Headers
    certificate: TLSCertificate
    heartbeat_interval_seconds: int
    def __init__(self, destination_endpoint: _Optional[str] = ..., headers: _Optional[_Union[Headers, _Mapping]] = ..., certificate: _Optional[_Union[TLSCertificate, _Mapping]] = ..., heartbeat_interval_seconds: _Optional[int] = ...) -> None: ...

class TelemetryConnectionSettings(_message.Message):
    __slots__ = ("destination_endpoint", "headers", "certificate")
    DESTINATION_ENDPOINT_FIELD_NUMBER: _ClassVar[int]
    HEADERS_FIELD_NUMBER: _ClassVar[int]
    CERTIFICATE_FIELD_NUMBER: _ClassVar[int]
    destination_endpoint: str
    headers: Headers
    certificate: TLSCertificate
    def __init__(self, destination_endpoint: _Optional[str] = ..., headers: _Optional[_Union[Headers, _Mapping]] = ..., certificate: _Optional[_Union[TLSCertificate, _Mapping]] = ...) -> None: ...

class OtherConnectionSettings(_message.Message):
    __slots__ = ("destination_endpoint", "headers", "certificate", "other_settings")
    class OtherSettingsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    DESTINATION_ENDPOINT_FIELD_NUMBER: _ClassVar[int]
    HEADERS_FIELD_NUMBER: _ClassVar[int]
    CERTIFICATE_FIELD_NUMBER: _ClassVar[int]
    OTHER_SETTINGS_FIELD_NUMBER: _ClassVar[int]
    destination_endpoint: str
    headers: Headers
    certificate: TLSCertificate
    other_settings: _containers.ScalarMap[str, str]
    def __init__(self, destination_endpoint: _Optional[str] = ..., headers: _Optional[_Union[Headers, _Mapping]] = ..., certificate: _Optional[_Union[TLSCertificate, _Mapping]] = ..., other_settings: _Optional[_Mapping[str, str]] = ...) -> None: ...

class Headers(_message.Message):
    __slots__ = ("headers",)
    HEADERS_FIELD_NUMBER: _ClassVar[int]
    headers: _containers.RepeatedCompositeFieldContainer[Header]
    def __init__(self, headers: _Optional[_Iterable[_Union[Header, _Mapping]]] = ...) -> None: ...

class Header(_message.Message):
    __slots__ = ("key", "value")
    KEY_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    key: str
    value: str
    def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

class TLSCertificate(_message.Message):
    __slots__ = ("cert", "private_key", "ca_cert")
    CERT_FIELD_NUMBER: _ClassVar[int]
    PRIVATE_KEY_FIELD_NUMBER: _ClassVar[int]
    CA_CERT_FIELD_NUMBER: _ClassVar[int]
    cert: bytes
    private_key: bytes
    ca_cert: bytes
    def __init__(self, cert: _Optional[bytes] = ..., private_key: _Optional[bytes] = ..., ca_cert: _Optional[bytes] = ...) -> None: ...

class ConnectionSettingsOffers(_message.Message):
    __slots__ = ("hash", "opamp", "own_metrics", "own_traces", "own_logs", "other_connections")
    class OtherConnectionsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: OtherConnectionSettings
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[OtherConnectionSettings, _Mapping]] = ...) -> None: ...
    HASH_FIELD_NUMBER: _ClassVar[int]
    OPAMP_FIELD_NUMBER: _ClassVar[int]
    OWN_METRICS_FIELD_NUMBER: _ClassVar[int]
    OWN_TRACES_FIELD_NUMBER: _ClassVar[int]
    OWN_LOGS_FIELD_NUMBER: _ClassVar[int]
    OTHER_CONNECTIONS_FIELD_NUMBER: _ClassVar[int]
    hash: bytes
    opamp: OpAMPConnectionSettings
    own_metrics: TelemetryConnectionSettings
    own_traces: TelemetryConnectionSettings
    own_logs: TelemetryConnectionSettings
    other_connections: _containers.MessageMap[str, OtherConnectionSettings]
    def __init__(self, hash: _Optional[bytes] = ..., opamp: _Optional[_Union[OpAMPConnectionSettings, _Mapping]] = ..., own_metrics: _Optional[_Union[TelemetryConnectionSettings, _Mapping]] = ..., own_traces: _Optional[_Union[TelemetryConnectionSettings, _Mapping]] = ..., own_logs: _Optional[_Union[TelemetryConnectionSettings, _Mapping]] = ..., other_connections: _Optional[_Mapping[str, OtherConnectionSettings]] = ...) -> None: ...

class PackagesAvailable(_message.Message):
    __slots__ = ("packages", "all_packages_hash")
    class PackagesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: PackageAvailable
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[PackageAvailable, _Mapping]] = ...) -> None: ...
    PACKAGES_FIELD_NUMBER: _ClassVar[int]
    ALL_PACKAGES_HASH_FIELD_NUMBER: _ClassVar[int]
    packages: _containers.MessageMap[str, PackageAvailable]
    all_packages_hash: bytes
    def __init__(self, packages: _Optional[_Mapping[str, PackageAvailable]] = ..., all_packages_hash: _Optional[bytes] = ...) -> None: ...

class PackageAvailable(_message.Message):
    __slots__ = ("type", "version", "file", "hash")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    FILE_FIELD_NUMBER: _ClassVar[int]
    HASH_FIELD_NUMBER: _ClassVar[int]
    type: PackageType
    version: str
    file: DownloadableFile
    hash: bytes
    def __init__(self, type: _Optional[_Union[PackageType, str]] = ..., version: _Optional[str] = ..., file: _Optional[_Union[DownloadableFile, _Mapping]] = ..., hash: _Optional[bytes] = ...) -> None: ...

class DownloadableFile(_message.Message):
    __slots__ = ("download_url", "content_hash", "signature", "headers")
    DOWNLOAD_URL_FIELD_NUMBER: _ClassVar[int]
    CONTENT_HASH_FIELD_NUMBER: _ClassVar[int]
    SIGNATURE_FIELD_NUMBER: _ClassVar[int]
    HEADERS_FIELD_NUMBER: _ClassVar[int]
    download_url: str
    content_hash: bytes
    signature: bytes
    headers: Headers
    def __init__(self, download_url: _Optional[str] = ..., content_hash: _Optional[bytes] = ..., signature: _Optional[bytes] = ..., headers: _Optional[_Union[Headers, _Mapping]] = ...) -> None: ...

class ServerErrorResponse(_message.Message):
    __slots__ = ("type", "error_message", "retry_info")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    RETRY_INFO_FIELD_NUMBER: _ClassVar[int]
    type: ServerErrorResponseType
    error_message: str
    retry_info: RetryInfo
    def __init__(self, type: _Optional[_Union[ServerErrorResponseType, str]] = ..., error_message: _Optional[str] = ..., retry_info: _Optional[_Union[RetryInfo, _Mapping]] = ...) -> None: ...

class RetryInfo(_message.Message):
    __slots__ = ("retry_after_nanoseconds",)
    RETRY_AFTER_NANOSECONDS_FIELD_NUMBER: _ClassVar[int]
    retry_after_nanoseconds: int
    def __init__(self, retry_after_nanoseconds: _Optional[int] = ...) -> None: ...

class ServerToAgentCommand(_message.Message):
    __slots__ = ("type",)
    TYPE_FIELD_NUMBER: _ClassVar[int]
    type: CommandType
    def __init__(self, type: _Optional[_Union[CommandType, str]] = ...) -> None: ...

class AgentDescription(_message.Message):
    __slots__ = ("identifying_attributes", "non_identifying_attributes")
    IDENTIFYING_ATTRIBUTES_FIELD_NUMBER: _ClassVar[int]
    NON_IDENTIFYING_ATTRIBUTES_FIELD_NUMBER: _ClassVar[int]
    identifying_attributes: _containers.RepeatedCompositeFieldContainer[_anyvalue_pb2.KeyValue]
    non_identifying_attributes: _containers.RepeatedCompositeFieldContainer[_anyvalue_pb2.KeyValue]
    def __init__(self, identifying_attributes: _Optional[_Iterable[_Union[_anyvalue_pb2.KeyValue, _Mapping]]] = ..., non_identifying_attributes: _Optional[_Iterable[_Union[_anyvalue_pb2.KeyValue, _Mapping]]] = ...) -> None: ...

class ComponentHealth(_message.Message):
    __slots__ = ("healthy", "start_time_unix_nano", "last_error", "status", "status_time_unix_nano", "component_health_map")
    class ComponentHealthMapEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: ComponentHealth
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[ComponentHealth, _Mapping]] = ...) -> None: ...
    HEALTHY_FIELD_NUMBER: _ClassVar[int]
    START_TIME_UNIX_NANO_FIELD_NUMBER: _ClassVar[int]
    LAST_ERROR_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STATUS_TIME_UNIX_NANO_FIELD_NUMBER: _ClassVar[int]
    COMPONENT_HEALTH_MAP_FIELD_NUMBER: _ClassVar[int]
    healthy: bool
    start_time_unix_nano: int
    last_error: str
    status: str
    status_time_unix_nano: int
    component_health_map: _containers.MessageMap[str, ComponentHealth]
    def __init__(self, healthy: bool = ..., start_time_unix_nano: _Optional[int] = ..., last_error: _Optional[str] = ..., status: _Optional[str] = ..., status_time_unix_nano: _Optional[int] = ..., component_health_map: _Optional[_Mapping[str, ComponentHealth]] = ...) -> None: ...

class EffectiveConfig(_message.Message):
    __slots__ = ("config_map",)
    CONFIG_MAP_FIELD_NUMBER: _ClassVar[int]
    config_map: AgentConfigMap
    def __init__(self, config_map: _Optional[_Union[AgentConfigMap, _Mapping]] = ...) -> None: ...

class RemoteConfigStatus(_message.Message):
    __slots__ = ("last_remote_config_hash", "status", "error_message")
    LAST_REMOTE_CONFIG_HASH_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    last_remote_config_hash: bytes
    status: RemoteConfigStatuses
    error_message: str
    def __init__(self, last_remote_config_hash: _Optional[bytes] = ..., status: _Optional[_Union[RemoteConfigStatuses, str]] = ..., error_message: _Optional[str] = ...) -> None: ...

class PackageStatuses(_message.Message):
    __slots__ = ("packages", "server_provided_all_packages_hash", "error_message")
    class PackagesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: PackageStatus
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[PackageStatus, _Mapping]] = ...) -> None: ...
    PACKAGES_FIELD_NUMBER: _ClassVar[int]
    SERVER_PROVIDED_ALL_PACKAGES_HASH_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    packages: _containers.MessageMap[str, PackageStatus]
    server_provided_all_packages_hash: bytes
    error_message: str
    def __init__(self, packages: _Optional[_Mapping[str, PackageStatus]] = ..., server_provided_all_packages_hash: _Optional[bytes] = ..., error_message: _Optional[str] = ...) -> None: ...

class PackageStatus(_message.Message):
    __slots__ = ("name", "agent_has_version", "agent_has_hash", "server_offered_version", "server_offered_hash", "status", "error_message", "download_details")
    NAME_FIELD_NUMBER: _ClassVar[int]
    AGENT_HAS_VERSION_FIELD_NUMBER: _ClassVar[int]
    AGENT_HAS_HASH_FIELD_NUMBER: _ClassVar[int]
    SERVER_OFFERED_VERSION_FIELD_NUMBER: _ClassVar[int]
    SERVER_OFFERED_HASH_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    DOWNLOAD_DETAILS_FIELD_NUMBER: _ClassVar[int]
    name: str
    agent_has_version: str
    agent_has_hash: bytes
    server_offered_version: str
    server_offered_hash: bytes
    status: PackageStatusEnum
    error_message: str
    download_details: PackageDownloadDetails
    def __init__(self, name: _Optional[str] = ..., agent_has_version: _Optional[str] = ..., agent_has_hash: _Optional[bytes] = ..., server_offered_version: _Optional[str] = ..., server_offered_hash: _Optional[bytes] = ..., status: _Optional[_Union[PackageStatusEnum, str]] = ..., error_message: _Optional[str] = ..., download_details: _Optional[_Union[PackageDownloadDetails, _Mapping]] = ...) -> None: ...

class PackageDownloadDetails(_message.Message):
    __slots__ = ("download_percent", "download_bytes_per_second")
    DOWNLOAD_PERCENT_FIELD_NUMBER: _ClassVar[int]
    DOWNLOAD_BYTES_PER_SECOND_FIELD_NUMBER: _ClassVar[int]
    download_percent: float
    download_bytes_per_second: float
    def __init__(self, download_percent: _Optional[float] = ..., download_bytes_per_second: _Optional[float] = ...) -> None: ...

class AgentIdentification(_message.Message):
    __slots__ = ("new_instance_uid",)
    NEW_INSTANCE_UID_FIELD_NUMBER: _ClassVar[int]
    new_instance_uid: bytes
    def __init__(self, new_instance_uid: _Optional[bytes] = ...) -> None: ...

class AgentRemoteConfig(_message.Message):
    __slots__ = ("config", "config_hash")
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    CONFIG_HASH_FIELD_NUMBER: _ClassVar[int]
    config: AgentConfigMap
    config_hash: bytes
    def __init__(self, config: _Optional[_Union[AgentConfigMap, _Mapping]] = ..., config_hash: _Optional[bytes] = ...) -> None: ...

class AgentConfigMap(_message.Message):
    __slots__ = ("config_map",)
    class ConfigMapEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: AgentConfigFile
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[AgentConfigFile, _Mapping]] = ...) -> None: ...
    CONFIG_MAP_FIELD_NUMBER: _ClassVar[int]
    config_map: _containers.MessageMap[str, AgentConfigFile]
    def __init__(self, config_map: _Optional[_Mapping[str, AgentConfigFile]] = ...) -> None: ...

class AgentConfigFile(_message.Message):
    __slots__ = ("body", "content_type")
    BODY_FIELD_NUMBER: _ClassVar[int]
    CONTENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    body: bytes
    content_type: str
    def __init__(self, body: _Optional[bytes] = ..., content_type: _Optional[str] = ...) -> None: ...

class CustomCapabilities(_message.Message):
    __slots__ = ("capabilities",)
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    capabilities: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, capabilities: _Optional[_Iterable[str]] = ...) -> None: ...

class CustomMessage(_message.Message):
    __slots__ = ("capability", "type", "data")
    CAPABILITY_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    capability: str
    type: str
    data: bytes
    def __init__(self, capability: _Optional[str] = ..., type: _Optional[str] = ..., data: _Optional[bytes] = ...) -> None: ...

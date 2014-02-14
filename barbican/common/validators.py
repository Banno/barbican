"""
API JSON validators.
"""

import abc

import jsonschema as schema
from oslo.config import cfg

from barbican.common import exception
from barbican.common import utils
from barbican.crypto import mime_types
from barbican.openstack.common import timeutils
from barbican.openstack.common.gettextutils import _


LOG = utils.getLogger(__name__)
DEFAULT_MAX_SECRET_BYTES = 10000
common_opts = [
    cfg.IntOpt('max_allowed_secret_in_bytes',
               default=DEFAULT_MAX_SECRET_BYTES),
]

CONF = cfg.CONF
CONF.register_opts(common_opts)


def secret_too_big(data):
    return len(data.encode('utf-8')) > CONF.max_allowed_secret_in_bytes


def get_invalid_property(validation_error):
    # we are interested in the second item which is the failed propertyName.
    if validation_error.schema_path and len(validation_error.schema_path) > 1:
        return validation_error.schema_path[1]


class ValidatorBase(object):
    """Base class for validators."""

    __metaclass__ = abc.ABCMeta
    name = ''

    @abc.abstractmethod
    def validate(self, json_data, parent_schema=None):
        """Validate the input JSON.

        :param json_data: JSON to validate against this class' internal schema.
        :param parent_schema: Name of the parent schema to this schema.
        :returns: dict -- JSON content, post-validation and
        :                 normalization/defaulting.
        :raises: schema.ValidationError on schema violations.

        """

    def _full_name(self, parent_schema=None):
        """
        Returns the full schema name for this validator,
        including parent name.
        """
        schema_name = self.name
        if parent_schema:
            schema_name = _("{0}' within '{1}").format(self.name,
                                                       parent_schema)
        return schema_name


class NewSecretValidator(ValidatorBase):
    """Validate a new secret."""

    def __init__(self):
        self.name = 'Secret'

        # TODO: Get the list of mime_types from the crypto plugins?
        self.schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "algorithm": {"type": "string"},
                "mode": {"type": "string"},
                "bit_length": {"type": "integer", "minimum": 0},
                "expiration": {"type": "string"},
                "payload": {"type": "string"},
                "payload_content_type": {
                    "type": "string",
                    "enum": mime_types.SUPPORTED
                },
                "payload_content_encoding": {
                    "type": "string",
                    "enum": [
                        "base64"
                    ]
                },
            },
        }

    def validate(self, json_data, parent_schema=None):
        schema_name = self._full_name(parent_schema)

        try:
            schema.validate(json_data, self.schema)
        except schema.ValidationError as e:
            raise exception.InvalidObject(schema=schema_name,
                                          reason=e.message,
                                          property=get_invalid_property(e))

        # Validate/normalize 'name'.
        name = json_data.get('name', '').strip()
        if not name:
            name = None
        json_data['name'] = name

        # Validate/convert 'expiration' if provided.
        expiration = self._extract_expiration(json_data, schema_name)
        if expiration:
            # Verify not already expired.
            utcnow = timeutils.utcnow()
            if expiration <= utcnow:
                raise exception.InvalidObject(schema=schema_name,
                                              reason=_("'expiration' is "
                                                       "before current time"),
                                              property="expiration")
        json_data['expiration'] = expiration

        # Validate/convert 'payload' if provided.
        if 'payload' in json_data:
            content_type = json_data.get('payload_content_type')
            if content_type is None:
                raise exception.InvalidObject(
                    schema=schema_name,
                    reason=_("If 'payload' is supplied, 'payload_content_type'"
                             " must also be supplied."),
                    property="payload_content_type"
                )

            content_encoding = json_data.get('payload_content_encoding')
            if content_type == 'application/octet-stream' and \
                    content_encoding is None:
                raise exception.InvalidObject(
                    schema=schema_name,
                    reason=_("payload_content_encoding must be specified "
                             "when payload_content_type is application/"
                             "octet-stream."),
                    property="payload_content_encoding"
                )

            if content_type.startswith('text/plain') and \
                    content_encoding is not None:
                raise exception.InvalidObject(
                    schema=schema_name,
                    reason=_("payload_content_encoding must not be specified "
                             "when payload_content_type is text/plain"),
                    property="payload_content_encoding"
                )

            payload = json_data['payload']
            if secret_too_big(payload):
                raise exception.LimitExceeded()

            payload = payload.strip()
            if not payload:
                raise exception.InvalidObject(schema=schema_name,
                                              reason=_("If 'payload' "
                                                       "specified, must be "
                                                       "non empty"),
                                              property="payload")

            json_data['payload'] = payload
        elif 'payload_content_type' in json_data and \
                parent_schema is None:
                raise exception.InvalidObject(
                    schema=schema_name,
                    reason=_("payload must be provided "
                             "when payload_content_type is specified"),
                    property="payload"
                )

        return json_data

    def _extract_expiration(self, json_data, schema_name):
        """Extracts and returns the expiration date from the JSON data."""
        expiration = None
        expiration_raw = json_data.get('expiration', None)
        if expiration_raw and expiration_raw.strip():
            try:
                expiration_tz = timeutils.parse_isotime(expiration_raw)
                expiration = timeutils.normalize_time(expiration_tz)
            except ValueError:
                LOG.exception("Problem parsing expiration date")
                raise exception.InvalidObject(schema=schema_name,
                                              reason=_("Invalid date "
                                                       "for 'expiration'"),
                                              property="expiration")

        return expiration


class NewOrderValidator(ValidatorBase):
    """Validate a new order."""

    def __init__(self):
        self.name = 'Order'
        self.schema = {
            "type": "object",
            "properties": {
            },
        }
        self.secret_validator = NewSecretValidator()

    def validate(self, json_data, parent_schema=None):
        schema_name = self._full_name(parent_schema)

        try:
            schema.validate(json_data, self.schema)
        except schema.ValidationError as e:
            raise exception.InvalidObject(schema=schema_name, reason=e.message,
                                          property=get_invalid_property(e))

        secret = json_data.get('secret')
        if secret is None:
            raise exception.InvalidObject(schema=schema_name,
                                          reason=_("'secret' attributes "
                                                   "are required"),
                                          property="secret")

        # If secret group is provided, validate it now.
        self.secret_validator.validate(secret, parent_schema=self.name)
        if 'payload' in secret:
            raise exception.InvalidObject(schema=schema_name,
                                          reason=_("'payload' not "
                                                   "allowed for secret "
                                                   "generation"),
                                          property="secret")

        # Validation secret generation related fields.
        # TODO: Invoke the crypto plugin for this purpose

        if secret.get('payload_content_type') != 'application/octet-stream':
            raise exception.UnsupportedField(field='payload_content_type',
                                             schema=schema_name,
                                             reason=_("Only 'application/oc"
                                                      "tet-stream' supported"))

        if secret.get('mode', '').lower() != 'cbc':
            raise exception.UnsupportedField(field="mode",
                                             schema=schema_name,
                                             reason=_("Only 'cbc' "
                                                      "supported"))

        if secret.get('algorithm', '').lower() != 'aes':
            raise exception.UnsupportedField(field="algorithm",
                                             schema=schema_name,
                                             reason=_("Only 'aes' "
                                                      "supported"))

        # TODO(reaperhulk): Future API change will move from bit to byte_length
        bit_length = int(secret.get('bit_length', 0))
        if bit_length <= 0:
            raise exception.UnsupportedField(field="bit_length",
                                             schema=schema_name,
                                             reason=_("Must have non-zero "
                                                      "positive bit_length "
                                                      "to generate secret"))
        if bit_length % 8 != 0:
            raise exception.UnsupportedField(field="bit_length",
                                             schema=schema_name,
                                             reason=_("Must be a positive "
                                                      "integer that is a "
                                                      "multiple of 8"))

        return json_data


class VerificationValidator(ValidatorBase):
    """Validate a verification resource request."""

    def __init__(self):
        self.name = 'Verification'
        self.schema = {
            "type": "object",
            "required": ["resource_type", "resource_ref",
                         "resource_action", "impersonation_allowed"],
            "properties": {
                "resource_type": {
                    "type": "string",
                    "enum": [
                        "image"
                    ]
                },
                "resource_ref": {"type": "string"},
                "resource_action": {
                    "type": "string",
                    "enum": [
                        "vm_attach"
                    ]
                },
                "impersonation_allowed": {"type": "boolean"},
            },
        }

    def validate(self, json_data, parent_schema=None):
        schema_name = self._full_name(parent_schema)

        try:
            schema.validate(json_data, self.schema)
        except schema.ValidationError as e:
            raise exception.InvalidObject(schema=schema_name, reason=e.message,
                                          property=get_invalid_property(e))

        return json_data


class VerificationExpectedValidator(ValidatorBase):
    """Validate a new secret."""

    def __init__(self):
        self.name = 'Verification Expected'

        self.schema = {
            "type": "object",
            "required": ["json_payload"],
            "properties": {
                "json_payload": {"type": "object"},
            },
        }

    def validate(self, json_data, parent_schema=None):
        schema_name = self._full_name(parent_schema)

        try:
            schema.validate(json_data, self.schema)
        except schema.ValidationError as e:
            raise exception.InvalidObject(schema=schema_name,
                                          reason=e.message,
                                          property=get_invalid_property(e))

        # Validate/normalize 'name'.
        payload = json_data.get('json_payload', '')
        if not payload:
            raise exception.InvalidObject(schema=schema_name,
                                          reason=_("'json_payload' must "
                                                   "be specified"),
                                          property="json_payload")

        try:
            size_payload = len(payload.keys())
        except AttributeError:
            raise exception.InvalidObject(schema=schema_name,
                                          reason=_("'json_payload' must "
                                                   "be a JSON object"),
                                          property="json_payload")

        if not size_payload:
            raise exception.InvalidObject(schema=schema_name,
                                          reason=_("'json_payload' must "
                                                   "have one or "
                                                   "more elements"),
                                          property="json_payload")

        return json_data

    def _extract_expiration(self, json_data, schema_name):
        """Extracts and returns the expiration date from the JSON data."""
        expiration = None
        expiration_raw = json_data.get('expiration', None)
        if expiration_raw and expiration_raw.strip():
            try:
                expiration_tz = timeutils.parse_isotime(expiration_raw)
                expiration = timeutils.normalize_time(expiration_tz)
            except ValueError:
                LOG.exception("Problem parsing expiration date")
                raise exception.InvalidObject(schema=schema_name,
                                              reason=_("Invalid date "
                                                       "for 'expiration'"),
                                              property="expiration")

        return expiration

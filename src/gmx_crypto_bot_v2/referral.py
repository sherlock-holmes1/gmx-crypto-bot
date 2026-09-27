"""Compatibility entry point; implementation lives in the layered V2 package."""

from gmx_crypto_bot_v2.checks.referral import compare_referral as compare_referral
from gmx_crypto_bot_v2.domain.keys import config_base_key as config_base_key
from gmx_crypto_bot_v2.domain.keys import keccak256 as keccak256
from gmx_crypto_bot_v2.domain.referral import PRO_BASES as PRO_BASES
from gmx_crypto_bot_v2.domain.referral import PRO_NAMES as PRO_NAMES
from gmx_crypto_bot_v2.domain.referral import SIGNATURES as SIGNATURES
from gmx_crypto_bot_v2.domain.referral import TOPICS as TOPICS
from gmx_crypto_bot_v2.domain.referral import ZERO as ZERO
from gmx_crypto_bot_v2.domain.referral import ZERO_CODE as ZERO_CODE
from gmx_crypto_bot_v2.domain.referral import P as P
from gmx_crypto_bot_v2.domain.referral import address as address
from gmx_crypto_bot_v2.domain.referral import calldata as calldata
from gmx_crypto_bot_v2.domain.referral import config_change as config_change
from gmx_crypto_bot_v2.domain.referral import datastore_key as datastore_key
from gmx_crypto_bot_v2.domain.referral import decode_referral_log as decode_referral_log
from gmx_crypto_bot_v2.domain.referral import word as word
from gmx_crypto_bot_v2.domain.referral import words as words
from gmx_crypto_bot_v2.models.referral import discount_amounts as discount_amounts
from gmx_crypto_bot_v2.reconstruction.referral import History as History
from gmx_crypto_bot_v2.reconstruction.referral import ReferralState as ReferralState

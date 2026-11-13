"""Payment providers (#400): the protocol, the registry and the adapters (Stripe, Mercado Pago, Asaas)."""

from .base import (
    Capability,
    ChargeResult,
    CheckoutSession,
    CustomerData,
    InvoiceData,
    PaymentProvider,
    PaymentState,
    PaymentStatus,
    ProviderError,
    RefundResult,
    VerifiedNotification,
    WebhookRejected,
    all_providers,
    get_provider,
    register_provider,
)

__all__ = [
    "Capability",
    "ChargeResult",
    "CheckoutSession",
    "CustomerData",
    "InvoiceData",
    "PaymentProvider",
    "PaymentState",
    "PaymentStatus",
    "ProviderError",
    "RefundResult",
    "VerifiedNotification",
    "WebhookRejected",
    "all_providers",
    "get_provider",
    "register_provider",
]

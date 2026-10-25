"""bridge_mail_categories.py — MailCategoriesMixin: tag emails with Outlook
categories as triggers handle them.

The trigger engine reports each trigger's lifecycle through its ``on_stage`` hook
(:meth:`_on_trigger_stage`): matched, awaiting claim, skipped, then done / failed
/ needs a person. A reply saved to Drafts reports ``reply_drafted``
(:meth:`_mail_stage_for_card`). For an Outlook ``new_mail`` event, when
``[mail_categories]`` (or the trigger's own ``categories``) is on, the change from
``mail_categories.stage_change`` is written to the email by
``sources.outlook.update_categories``.

Writes run on one background thread, in order, so a slow Outlook never holds up
a trigger run and a later stage can't land before an earlier one. Every failure
is logged and dropped: a category is a courtesy, never a reason to fail a run.
The claim's own ``Claimed: <name>`` stamp is written by ``claim_message`` (see
``bridge_triggers.claim_email``). Settings: Settings → Mailbox → Outlook
categories (:meth:`get_mail_categories` / :meth:`save_mail_categories`).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from . import config as config_mod
from . import mail_categories as mc
from .bridge_const import logger

if TYPE_CHECKING:
    from .events import Event
    from .triggers import Trigger, TriggerCard

# Cards whose email may still get a stage reported outside the engine hook (a
# reply draft) — card id → (trigger, event). Bounded: parked claims can sit a
# long time, and the oldest entries are dropped first.
_CTX_CAP = 500

_init_lock = threading.Lock()


class MailCategoriesMixin:
    _mail_cat_pool: ThreadPoolExecutor | None
    _mail_cat_ctx: dict[str, tuple[Trigger, Event]]

    # ── JS API ──

    def get_mail_categories(self) -> dict[str, Any]:
        """The ``[mail_categories]`` settings: ``{enabled, prefix, colors}``."""
        return config_mod.read_mail_categories()

    def save_mail_categories(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Save the ``[mail_categories]`` settings; returns what was saved. Takes
        effect on the next stage (no trigger restart needed)."""
        return config_mod.write_mail_categories(dict(settings or {}))

    # ── engine hook + helpers ──

    def _mail_cat_state(self) -> tuple[ThreadPoolExecutor, dict[str, Any]]:
        with _init_lock:
            if getattr(self, "_mail_cat_pool", None) is None:
                self._mail_cat_pool = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="mail-categories"
                )
                self._mail_cat_ctx = {}
            assert self._mail_cat_pool is not None
            return self._mail_cat_pool, self._mail_cat_ctx

    @staticmethod
    def _mail_cat_applies(trigger: Trigger, event: Event, enabled: bool) -> bool:
        """Whether this trigger's email gets categories: an Outlook ``new_mail``
        with an entry id, and the trigger's override (or else the setting) on."""
        if event.source != "outlook" or event.type != "new_mail":
            return False
        if not str(event.payload.get("entry_id") or ""):
            return False
        return trigger.categories if trigger.categories is not None else enabled

    def _on_trigger_stage(
        self,
        stage: str,
        trigger: Trigger,
        event: Event,
        card: TriggerCard | None,
    ) -> None:
        """TriggerEngine ``on_stage`` hook. Cheap and non-blocking: it records the
        card's context and queues the write."""
        if event.source != "outlook" or event.type != "new_mail":
            return
        _pool, ctx = self._mail_cat_state()
        if card is not None:
            with _init_lock:
                if stage in (mc.MATCHED, mc.AWAITING_CLAIM):
                    ctx[card.id] = (trigger, event)
                    while len(ctx) > _CTX_CAP:
                        ctx.pop(next(iter(ctx)))
                elif stage in (mc.DONE, mc.FAILED, mc.NEEDS_PERSON):
                    ctx.pop(card.id, None)
        self._queue_mail_stage(stage, trigger, event)

    def _mail_stage_for_card(self, card_id: str, stage: str) -> None:
        """Report ``stage`` for the email behind trigger card ``card_id`` (used for
        ``reply_drafted``, which happens inside an agent turn). No-op for a card
        that isn't an email trigger."""
        _pool, ctx = self._mail_cat_state()
        with _init_lock:
            found = ctx.get(str(card_id or ""))
        if found is not None:
            self._queue_mail_stage(stage, found[0], found[1])

    def _queue_mail_stage(self, stage: str, trigger: Trigger, event: Event) -> None:
        pool, _ctx = self._mail_cat_state()
        try:
            pool.submit(self._write_mail_stage, stage, trigger, event)
        except RuntimeError:
            logger.debug("mail categories: pool closed, %s dropped", stage)

    def _write_mail_stage(self, stage: str, trigger: Trigger, event: Event) -> None:
        """Worker: read the settings, then write the stage's change to the email.
        The entry id is read now, so a stage after a claim targets the moved email
        (``claim_email`` updates the payload)."""
        try:
            settings = config_mod.read_mail_categories()
            if not self._mail_cat_applies(trigger, event, settings["enabled"]):
                return
            change = mc.stage_change(stage, settings["prefix"], trigger.name)
            if change.empty:
                return
            from .sources import outlook as ol

            if not ol.outlook_available():
                return
            entry_id = str(event.payload.get("entry_id") or "")
            ol.update_categories(
                entry_id,
                add=change.add,
                status=change.status,
                statuses=mc.status_categories(settings["prefix"]),
                weak=change.weak,
                remove=change.remove,
                colors=change.colors if settings["colors"] else None,
            )
        except Exception:
            logger.info(
                "mail categories: %s for trigger %r not written",
                stage,
                trigger.name,
                exc_info=True,
            )

    def _claim_category_args(self, owner: str) -> dict[str, Any]:
        """Extra ``claim_message`` kwargs: drop the lifecycle status in the same
        save as the ``Claimed:`` stamp, and colour the claimer's category when
        colours are on. Statuses are removed even when categories are off here,
        since another console may have set them."""
        from .sources import outlook as ol

        try:
            settings = config_mod.read_mail_categories()
        except Exception:
            settings = mc.read_settings(None)
        args: dict[str, Any] = {"remove": mc.status_categories(settings["prefix"])}
        if settings["enabled"] and settings["colors"]:
            args["colors"] = {ol._claim_category(owner): mc.CLAIMED_COLOR}
        return args

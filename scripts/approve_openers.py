"""
CLI for reviewing and approving openers without Discord.

Usage (inside the worker container):
  docker compose exec worker_qual python -m scripts.approve_openers list
  docker compose exec worker_qual python -m scripts.approve_openers show <opener_id>
  docker compose exec worker_qual python -m scripts.approve_openers approve <opener_id> [--by NAME]
  docker compose exec worker_qual python -m scripts.approve_openers edit <opener_id> "<new text>" [--by NAME]
  docker compose exec worker_qual python -m scripts.approve_openers reject <opener_id> [--reason TEXT]

Or, for batch ops:
  docker compose exec worker_qual python -m scripts.approve_openers approve-all-under <follower_cap> --by NAME
  docker compose exec worker_qual python -m scripts.approve_openers reject-celebrities --cap 500000
"""
from __future__ import annotations
import argparse
import os
import sys
from datetime import datetime, timezone

from app.core.supabase_client import get_supabase  # type: ignore


def _print_opener_row(o: dict, prospect: dict | None = None) -> None:
    handle = prospect["handle"] if prospect else "?"
    fc = prospect.get("follower_count") if prospect else None
    score = prospect.get("total_score") if prospect else None
    fc_str = f"{fc:,}" if fc else "?"
    flags = "".join([
        "S" if o.get("sipe_short") else "-",
        "I" if o.get("sipe_incomplete") else "-",
        "P" if o.get("sipe_personal") else "-",
        "E" if o.get("sipe_emotional") else "-",
    ])
    approved = "✓" if o.get("approved_for_send") else " "
    sent = "→" if o.get("sent_at") else " "
    print(
        f"[{approved}{sent}] {o['id'][:8]}  @{handle:<28} "
        f"score={score:<3}  fc={fc_str:>10}  sipe={flags}  "
        f"hook={(o.get('hooked_on_gap') or '?')[:24]}"
    )
    text = (o.get("opener_text") or "").strip()
    print(f"          {text!r}")
    print()


def cmd_list(args: argparse.Namespace) -> int:
    sb = get_supabase()
    q = sb.table("openers").select("*")
    if args.pending:
        q = q.eq("approved_for_send", False).is_("sent_at", "null")
    elif args.approved:
        q = q.eq("approved_for_send", True).is_("sent_at", "null")
    elif args.sent:
        q = q.not_.is_("sent_at", "null")
    q = q.order("generated_at", desc=True).limit(args.limit)
    openers = q.execute().data or []

    if not openers:
        print("(no openers match the filter)")
        return 0

    # Bulk-fetch prospects to avoid N+1
    prospect_ids = [o["prospect_id"] for o in openers]
    prospects = (sb.table("qualified_prospects").select("*")
                 .in_("id", prospect_ids).execute()).data or []
    by_id = {p["id"]: p for p in prospects}

    print(f"Found {len(openers)} openers:\n")
    for o in openers:
        _print_opener_row(o, by_id.get(o["prospect_id"]))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    sb = get_supabase()
    o = (sb.table("openers").select("*").eq("id", args.opener_id)
         .single().execute()).data
    if not o:
        print(f"No opener {args.opener_id}", file=sys.stderr)
        return 1
    p = (sb.table("qualified_prospects").select("*")
         .eq("id", o["prospect_id"]).single().execute()).data
    _print_opener_row(o, p)

    print("--- Full prospect ---")
    for k in ("handle", "follower_count", "total_score", "pre_filter_score",
              "link_crawl_score", "cross_platform_score", "is_high_value",
              "status", "link_in_bio", "link_resolved_to"):
        v = p.get(k) if p else None
        print(f"  {k}: {v}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    sb = get_supabase()
    r = (sb.table("openers").update({
        "approved_for_send": True,
        "approved_by": args.by,
        "approved_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", args.opener_id).execute())
    if not r.data:
        print(f"No opener {args.opener_id}", file=sys.stderr)
        return 1
    print(f"✓ approved {args.opener_id} (by {args.by})")
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    sb = get_supabase()
    if len(args.new_text) > 280:
        print(f"WARNING: opener is {len(args.new_text)} chars (>280, Mason recommends ≤160 for openers)")
    r = (sb.table("openers").update({
        "opener_text": args.new_text,
    }).eq("id", args.opener_id).execute())
    if not r.data:
        print(f"No opener {args.opener_id}", file=sys.stderr)
        return 1
    print(f"✓ edited opener_text on {args.opener_id}")
    if args.then_approve:
        return cmd_approve(args)
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    sb = get_supabase()
    # We don't have a 'rejected' column — use approved_by + a flag-style note.
    # Easier: leave approved_for_send=false (default) and just log a reason.
    # To make rejection visible, set send_failure_reason to mark it.
    r = (sb.table("openers").update({
        "approved_for_send": False,
        "send_failure_reason": f"rejected: {args.reason or 'no reason given'}",
    }).eq("id", args.opener_id).execute())
    if not r.data:
        print(f"No opener {args.opener_id}", file=sys.stderr)
        return 1
    print(f"✗ rejected {args.opener_id} — {args.reason or '(no reason)'}")
    return 0


def cmd_approve_all_under(args: argparse.Namespace) -> int:
    sb = get_supabase()
    # Pending openers
    openers = (sb.table("openers").select("id,prospect_id")
               .eq("approved_for_send", False)
               .is_("sent_at", "null").execute()).data or []
    if not openers:
        print("No pending openers")
        return 0

    prospect_ids = [o["prospect_id"] for o in openers]
    prospects = (sb.table("qualified_prospects").select("id,handle,follower_count")
                 .in_("id", prospect_ids).execute()).data or []
    by_id = {p["id"]: p for p in prospects}

    approved_count = 0
    for o in openers:
        p = by_id.get(o["prospect_id"])
        if not p:
            continue
        fc = p.get("follower_count") or 0
        if fc <= args.follower_cap:
            sb.table("openers").update({
                "approved_for_send": True,
                "approved_by": args.by,
                "approved_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", o["id"]).execute()
            print(f"  ✓ approved {o['id'][:8]} @{p['handle']} (fc={fc:,})")
            approved_count += 1
    print(f"\nApproved {approved_count} openers under follower_count={args.follower_cap}")
    return 0


def cmd_reject_celebrities(args: argparse.Namespace) -> int:
    sb = get_supabase()
    openers = (sb.table("openers").select("id,prospect_id")
               .eq("approved_for_send", False)
               .is_("sent_at", "null").execute()).data or []
    prospect_ids = [o["prospect_id"] for o in openers]
    prospects = (sb.table("qualified_prospects").select("id,handle,follower_count")
                 .in_("id", prospect_ids).execute()).data or []
    by_id = {p["id"]: p for p in prospects}

    rejected = 0
    for o in openers:
        p = by_id.get(o["prospect_id"])
        if not p:
            continue
        fc = p.get("follower_count") or 0
        if fc > args.cap:
            sb.table("openers").update({
                "send_failure_reason": f"rejected: celebrity tier (fc={fc:,} > {args.cap:,})"
            }).eq("id", o["id"]).execute()
            print(f"  ✗ rejected {o['id'][:8]} @{p['handle']} (fc={fc:,})")
            rejected += 1
    print(f"\nRejected {rejected} celebrity-tier openers (cap={args.cap:,})")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Review and approve M6 openers without Discord")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List openers")
    g = p_list.add_mutually_exclusive_group()
    g.add_argument("--pending", action="store_true", help="only unapproved+unsent (default)")
    g.add_argument("--approved", action="store_true", help="only approved+unsent")
    g.add_argument("--sent", action="store_true", help="only already-sent")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.set_defaults(func=cmd_list, pending=True)

    p_show = sub.add_parser("show", help="Show full details for one opener")
    p_show.add_argument("opener_id")
    p_show.set_defaults(func=cmd_show)

    p_approve = sub.add_parser("approve", help="Approve an opener for sending")
    p_approve.add_argument("opener_id")
    p_approve.add_argument("--by", default=os.environ.get("USER", "cli"))
    p_approve.set_defaults(func=cmd_approve)

    p_edit = sub.add_parser("edit", help="Edit opener_text (optionally approve in same step)")
    p_edit.add_argument("opener_id")
    p_edit.add_argument("new_text")
    p_edit.add_argument("--by", default=os.environ.get("USER", "cli"))
    p_edit.add_argument("--then-approve", action="store_true",
                        help="approve in the same step")
    p_edit.set_defaults(func=cmd_edit)

    p_reject = sub.add_parser("reject", help="Mark an opener as rejected")
    p_reject.add_argument("opener_id")
    p_reject.add_argument("--reason")
    p_reject.set_defaults(func=cmd_reject)

    p_bulk = sub.add_parser("approve-all-under",
                            help="Bulk-approve all pending openers whose prospect has follower_count <= cap")
    p_bulk.add_argument("follower_cap", type=int)
    p_bulk.add_argument("--by", default=os.environ.get("USER", "cli"))
    p_bulk.set_defaults(func=cmd_approve_all_under)

    p_reject_celebs = sub.add_parser("reject-celebrities",
                                     help="Bulk-reject pending openers above follower cap")
    p_reject_celebs.add_argument("--cap", type=int, default=500000)
    p_reject_celebs.set_defaults(func=cmd_reject_celebrities)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

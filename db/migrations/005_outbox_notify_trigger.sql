-- Wake outbox consumers via LISTEN/NOTIFY when rows become PENDING (insert or requeue).
-- Channel naming must match common.messaging.notify.topic_to_notify_channel.

CREATE OR REPLACE FUNCTION notify_outbox_pending()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  sanitized text;
  channel text;
  channel_bytes bytea;
BEGIN
  sanitized := regexp_replace(lower(NEW.destination_topic), '[^a-z0-9_]', '_', 'g');
  channel := 'warden_' || sanitized;
  channel_bytes := convert_to(channel, 'UTF8');
  IF octet_length(channel_bytes) > 63 THEN
    -- md5(text) returns 32-char hex; matches hashlib.md5(...).hexdigest() in Python.
    channel := 'warden_' || md5(channel);
  END IF;
  PERFORM pg_notify(channel, '');
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS outbox_events_notify_pending ON outbox_events;

CREATE TRIGGER outbox_events_notify_pending
  AFTER INSERT OR UPDATE OF status ON outbox_events
  FOR EACH ROW
  WHEN (NEW.status = 'PENDING')
  EXECUTE PROCEDURE notify_outbox_pending();

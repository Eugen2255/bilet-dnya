ALTER TABLE courses ADD COLUMN schedule_from timestamptz;
UPDATE courses SET schedule_from = statement_timestamp() WHERE state = 'active';

CREATE FUNCTION record_course_schedule_start() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state = 'active' THEN
        IF TG_OP = 'INSERT' THEN
            NEW.schedule_from := clock_timestamp();
        ELSIF OLD.state <> 'active' THEN
            NEW.schedule_from := clock_timestamp();
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER courses_record_schedule_start
    BEFORE INSERT OR UPDATE OF state ON courses
    FOR EACH ROW EXECUTE FUNCTION record_course_schedule_start();

ALTER TABLE attempts ADD COLUMN graded_at timestamptz;

-- The original schema did not record when each legacy result became available.
UPDATE attempts SET graded_at = statement_timestamp() WHERE status = 'graded' AND result IS NOT NULL;

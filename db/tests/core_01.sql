\set ON_ERROR_STOP on
BEGIN;

-- Run after migrate.sql and seed.sql. All test writes are rolled back.
DO $$
DECLARE
    fixture_course CONSTANT uuid := '40000000-0000-4000-8000-000000000001';
    fixture_group CONSTANT uuid := '10000000-0000-4000-8000-000000000001';
    fixture_student CONSTANT uuid := '30000000-0000-4000-8000-000000000002';
    fixture_day CONSTANT date := DATE '2035-01-20';
    draft_course uuid;
    other_group uuid;
    other_member uuid;
    first_ticket uuid;
    first_rubric uuid;
    second_rubric uuid;
    new_assignment uuid;
    rejected_constraint text;
BEGIN
    ASSERT (SELECT state FROM courses WHERE id = fixture_course) = 'active';
    ASSERT (SELECT count(*) FROM tickets WHERE course_id = fixture_course) = 3;
    ASSERT (SELECT count(*) FROM memberships WHERE group_id = fixture_group) = 2;

    SELECT id, approved_rubric_id INTO STRICT first_ticket, first_rubric
    FROM tickets WHERE course_id = fixture_course AND ordinal = 1;
    SELECT approved_rubric_id INTO STRICT second_rubric
    FROM tickets WHERE course_id = fixture_course AND ordinal = 2;

    INSERT INTO study_groups (telegram_chat_id) VALUES (-1009000000002) RETURNING id INTO other_group;
    INSERT INTO memberships (group_id, user_id, role, alias)
    SELECT other_group, user_id, 'student', 'Проверка доступа'
    FROM memberships WHERE id = fixture_student RETURNING id INTO other_member;
    INSERT INTO courses (group_id, title)
    VALUES (other_group, 'Черновик') RETURNING id INTO draft_course;

    BEGIN
        UPDATE courses SET state = 'active' WHERE id = draft_course;
        RAISE EXCEPTION 'Activated an incomplete draft';
    EXCEPTION WHEN check_violation THEN
        GET STACKED DIAGNOSTICS rejected_constraint = CONSTRAINT_NAME;
        ASSERT rejected_constraint = 'course_schedule_complete';
    END;

    UPDATE courses
    SET exam_date = current_date + 30, timezone = 'UTC',
        morning_at = '09:00', evening_at = '20:00',
        weekdays = ARRAY[1, 2, 3, 4, 5, 6, 7]::smallint[]
    WHERE id = draft_course;
    BEGIN
        UPDATE courses SET state = 'active' WHERE id = draft_course;
        RAISE EXCEPTION 'Activated a course without tickets';
    EXCEPTION WHEN check_violation THEN
        GET STACKED DIAGNOSTICS rejected_constraint = CONSTRAINT_NAME;
        ASSERT rejected_constraint = 'course_activation_rubrics';
    END;

    BEGIN
        INSERT INTO assignments (course_id, group_id, member_id, ticket_id, day, rubric_id, kind, state)
        VALUES (fixture_course, fixture_group, other_member, first_ticket, fixture_day, first_rubric, 'daily', 'open');
        RAISE EXCEPTION 'Accepted a member from another group';
    EXCEPTION WHEN foreign_key_violation THEN
        GET STACKED DIAGNOSTICS rejected_constraint = CONSTRAINT_NAME;
        ASSERT rejected_constraint = 'assignments_member_id_group_id_fkey';
    END;
    BEGIN
        INSERT INTO assignments (course_id, group_id, member_id, ticket_id, day, rubric_id, kind, state)
        VALUES (fixture_course, fixture_group, fixture_student, first_ticket, fixture_day, second_rubric, 'daily', 'open');
        RAISE EXCEPTION 'Accepted a rubric from another ticket';
    EXCEPTION WHEN foreign_key_violation THEN
        GET STACKED DIAGNOSTICS rejected_constraint = CONSTRAINT_NAME;
        ASSERT rejected_constraint = 'assignments_rubric_id_ticket_id_fkey';
    END;

    INSERT INTO assignments (course_id, group_id, member_id, ticket_id, day, rubric_id, kind, state)
    VALUES (fixture_course, fixture_group, fixture_student, first_ticket, fixture_day, first_rubric, 'daily', 'open')
    RETURNING id INTO new_assignment;
    INSERT INTO attempts (assignment_id, telegram_update_id, body, status, result)
    VALUES (new_assignment, 900000000010, 'Тестовый ответ', 'graded', '{"score": 3}');
    BEGIN
        INSERT INTO attempts (assignment_id, telegram_update_id, body, status)
        VALUES (new_assignment, 900000000011, 'Повторный ответ', 'submitted');
        RAISE EXCEPTION 'Accepted a second attempt for the same assignment';
    EXCEPTION WHEN unique_violation THEN
        GET STACKED DIAGNOSTICS rejected_constraint = CONSTRAINT_NAME;
        ASSERT rejected_constraint = 'attempts_assignment_id_key';
    END;
    BEGIN
        UPDATE attempts SET result = '{"score": 6}' WHERE assignment_id = new_assignment;
        RAISE EXCEPTION 'Accepted a score greater than 5';
    EXCEPTION WHEN check_violation THEN
        GET STACKED DIAGNOSTICS rejected_constraint = CONSTRAINT_NAME;
        ASSERT rejected_constraint = 'attempt_score_range';
    END;

    DELETE FROM courses WHERE id = fixture_course;
    ASSERT NOT EXISTS (SELECT FROM assignments WHERE id = new_assignment);
    ASSERT NOT EXISTS (SELECT FROM attempts WHERE assignment_id = new_assignment);
    ASSERT NOT EXISTS (SELECT FROM rubrics WHERE id = first_rubric);
END;
$$;

ROLLBACK;

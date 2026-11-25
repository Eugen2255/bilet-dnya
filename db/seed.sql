\set ON_ERROR_STOP on
BEGIN;

INSERT INTO study_groups (id, telegram_chat_id)
VALUES ('10000000-0000-4000-8000-000000000001', -1009000000001)
ON CONFLICT (id) DO NOTHING;

INSERT INTO users (id, telegram_user_id)
VALUES ('20000000-0000-4000-8000-000000000001', 900000000001),
       ('20000000-0000-4000-8000-000000000002', 900000000002)
ON CONFLICT (id) DO NOTHING;

INSERT INTO memberships (id, group_id, user_id, role, alias)
VALUES
    ('30000000-0000-4000-8000-000000000001', '10000000-0000-4000-8000-000000000001',
     '20000000-0000-4000-8000-000000000001', 'organizer', 'Организатор'),
    ('30000000-0000-4000-8000-000000000002', '10000000-0000-4000-8000-000000000001',
     '20000000-0000-4000-8000-000000000002', 'student', 'Студент')
ON CONFLICT (id) DO NOTHING;

WITH inserted AS (
    INSERT INTO courses (id, group_id, title, exam_date, timezone, morning_at, evening_at, weekdays)
    VALUES (
        '40000000-0000-4000-8000-000000000001', '10000000-0000-4000-8000-000000000001',
        'Тестовый курс по базам данных', DATE '2035-01-31', 'Asia/Novosibirsk',
        TIME '09:00', TIME '20:00', ARRAY[1, 2, 3, 4, 5, 6, 7]::smallint[]
    )
    ON CONFLICT (id) DO NOTHING
    RETURNING id
)
SELECT EXISTS (SELECT FROM inserted) AS seed_new_course \gset

\if :seed_new_course
    INSERT INTO tickets (id, course_id, ordinal, question, state)
    VALUES
        ('50000000-0000-4000-8000-000000000001', '40000000-0000-4000-8000-000000000001',
         1, 'Что такое первичный ключ?', 'approved'),
        ('50000000-0000-4000-8000-000000000002', '40000000-0000-4000-8000-000000000001',
         2, 'Что такое внешний ключ?', 'approved'),
        ('50000000-0000-4000-8000-000000000003', '40000000-0000-4000-8000-000000000001',
         3, 'Зачем нужна транзакция?', 'approved');

    INSERT INTO rubrics (id, ticket_id, version, state, answer, source_status, points)
    VALUES
        ('60000000-0000-4000-8000-000000000001', '50000000-0000-4000-8000-000000000001',
         1, 'approved', 'Первичный ключ однозначно идентифицирует строку. Значения уникальны, NULL недопустим.',
         'unsourced', '[
            {"id":"p1","text":"Идентифицирует строку","citation":null},
            {"id":"p2","text":"Значения ключа уникальны","citation":null},
            {"id":"p3","text":"Ключ не содержит NULL","citation":null}
         ]'),
        ('60000000-0000-4000-8000-000000000002', '50000000-0000-4000-8000-000000000002',
         1, 'approved', 'Внешний ключ ссылается на ключ другой или той же таблицы. СУБД проверяет существование ссылки и поддерживает ссылочную целостность.',
         'unsourced', '[
            {"id":"p1","text":"Ссылается на ключ","citation":null},
            {"id":"p2","text":"СУБД проверяет существование ссылки","citation":null},
            {"id":"p3","text":"Поддерживает ссылочную целостность","citation":null}
         ]'),
        ('60000000-0000-4000-8000-000000000003', '50000000-0000-4000-8000-000000000003',
         1, 'approved', 'Транзакция объединяет операции. При подтверждении изменения фиксируются вместе, при откате отменяются.',
         'unsourced', '[
            {"id":"p1","text":"Объединяет операции","citation":null},
            {"id":"p2","text":"Подтверждение фиксирует изменения вместе","citation":null},
            {"id":"p3","text":"Откат отменяет изменения","citation":null}
         ]');

    UPDATE tickets AS ticket
    SET approved_rubric_id = rubric.id
    FROM rubrics AS rubric
    WHERE ticket.course_id = '40000000-0000-4000-8000-000000000001'
        AND rubric.ticket_id = ticket.id AND rubric.version = 1;

    UPDATE courses SET state = 'active' WHERE id = '40000000-0000-4000-8000-000000000001';
\endif

COMMIT;

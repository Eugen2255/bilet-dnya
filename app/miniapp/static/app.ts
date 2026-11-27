type Role = "organizer" | "teacher" | "student";
type CourseStatus = "draft" | "active" | "paused" | "closed";
type CourseChoice = { id: string; title: string; status: CourseStatus };
type Group = { id: string; title: string; role: Role; activeCourseId: string | null; courses: CourseChoice[] };
type Course = CourseChoice & {
    examDate: string | null; timezone: string | null; morningAt: string | null;
    eveningAt: string | null; weekdays: number[];
};
type Question = { ordinal: number; text: string };
type ApiError = { code: string; message: string; filename?: string; page?: number; dailyTarget?: number; projectedMissing?: number };
type ImportView = { importId: string; status: string; questions: Question[]; warnings: string[]; error: ApiError | null };
type Citation = { chunkId: string; quote: string };
type Point = { id: string; text: string; citation: Citation | null };
type Ticket = {
    id: string; question: string; version: number; status: string; approvedVersion: number | null; reviewRequired: boolean;
    rubric: { answer: string; sourceStatus: string; points: Point[] } | null;
};
type ProgressTicket = { id: string; question: string; passes: number; lastScore: number | null;
    lastAttemptId: string | null; nextReviewOn: string | null };
type Dashboard = { streak: number; tickets: ProgressTicket[]; group?: {
    active: number; responded: number; weakTickets: { id: string; count: number; mean: number }[];
} };
type Attempt = { status: string; score: number | null; covered: string[] | null; missing: string[] | null;
    errors: string[] | null; feedback: string | null; reference: string | null };
type RequestOptions = { method?: string; body?: object | FormData; key?: string };

declare global {
    interface Window {
        Telegram?: { WebApp?: { initData: string; ready(): void; expand(): void } };
    }
}

class RequestError extends Error {
    constructor(readonly detail: ApiError) { super(detail.message); }
}

const main = document.querySelector<HTMLDivElement>("#content")!;
const banner = document.querySelector<HTMLParagraphElement>("#notice")!;
const roleLabel = document.querySelector<HTMLElement>("#role")!;
const modal = document.querySelector<HTMLDialogElement>("#detail")!;
const modalTitle = document.querySelector<HTMLHeadingElement>("#detail-title")!;
const modalBody = document.querySelector<HTMLDivElement>("#detail-content")!;
const labels: Record<CourseStatus, string> = { draft: "Черновик", active: "Идёт тренировка", paused: "На паузе", closed: "Закрыт" };
const roles: Record<Role, string> = { organizer: "Организатор", teacher: "Преподаватель", student: "Студент" };
const week = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
const allowed = ".txt,.pdf,.docx,.jpg,.jpeg,.png";
const imports = new Map<string, string>();
let csrf = "";
let groups: Group[] = [];
let selectedGroup = "";
let selectedCourse = "";
let tab: "progress" | "setup" = "progress";
let renderToken = 0;

function make<K extends keyof HTMLElementTagNameMap>(
    tag: K, className = "", ...children: (Node | string)[]
): HTMLElementTagNameMap[K] {
    const node = document.createElement(tag);
    node.className = className;
    node.append(...children);
    return node;
}

function button(text: string, className = ""): HTMLButtonElement {
    const element = make("button", className, text);
    element.type = "button";
    return element;
}

function action(text: string, handler: () => Promise<void>, className = ""): HTMLButtonElement {
    const element = button(text, className);
    element.onclick = () => busy(element, handler);
    return element;
}

function field(title: string, control: HTMLElement): HTMLLabelElement {
    return make("label", "field", make("span", "", title), control);
}

function input(type: string, value = ""): HTMLInputElement {
    const element = make("input");
    element.type = type;
    element.value = value;
    return element;
}

function textarea(value = "", rows = 3): HTMLTextAreaElement {
    const element = make("textarea");
    element.rows = rows;
    element.value = value;
    return element;
}

function select(values: { id: string; label: string }[], value: string): HTMLSelectElement {
    const element = make("select");
    for (const item of values) element.append(new Option(item.label, item.id));
    element.value = value;
    return element;
}

function panel(title: string, ...nodes: Node[]): HTMLElement {
    return make("section", "panel", make("h2", "", title), ...nodes);
}

function notice(text = "", error = false): void {
    banner.textContent = text;
    banner.classList.toggle("error", error);
    banner.hidden = !text;
    if (error) banner.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function errorText(error: unknown): string {
    if (!(error instanceof RequestError)) return error instanceof Error ? error.message : "Сбой приложения.";
    const value = error.detail;
    return [value.message, value.filename, value.page ? "Страница " + value.page : null].filter(Boolean).join(" · ");
}

function busy(control: HTMLButtonElement, handler: () => Promise<void>): void {
    if (control.disabled) return;
    control.disabled = true;
    notice();
    Promise.resolve().then(handler).catch(error => notice(errorText(error), true)).finally(() => { control.disabled = false; });
}

async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const headers = new Headers();
    let body: string | FormData | undefined;
    if (options.method && options.method !== "GET") {
        if (csrf) headers.set("X-CSRF-Token", csrf);
        if (options.key) headers.set("Idempotency-Key", options.key);
    }
    if (options.body instanceof FormData) body = options.body;
    else if (options.body) {
        body = JSON.stringify(options.body);
        headers.set("Content-Type", "application/json");
    }
    let response: Response;
    try {
        response = await fetch("/v1" + path, { method: options.method || "GET", headers, body, credentials: "same-origin" });
    } catch {
        throw new Error("Связь с сервером потеряна. Попробуйте ещё раз.");
    }
    const data = await response.json().catch(() => null);
    if (!response.ok) throw new RequestError(data?.error || { code: "REQUEST_FAILED", message: "Не удалось выполнить запрос." });
    return data as T;
}

function submit(form: HTMLFormElement, label: string, handler: () => Promise<void>): HTMLButtonElement {
    const control = button(label, "primary");
    control.type = "submit";
    form.onsubmit = event => { event.preventDefault(); busy(control, handler); };
    form.append(control);
    return control;
}

function readStoredImport(courseId: string): string | undefined {
    try { return imports.get(courseId) || sessionStorage.getItem("bilet-import-" + courseId) || undefined; }
    catch { return imports.get(courseId); }
}

function saveStoredImport(courseId: string, importId: string): void {
    imports.set(courseId, importId);
    try { sessionStorage.setItem("bilet-import-" + courseId, importId); } catch { /* WebView can disable storage. */ }
}

async function loadGroups(): Promise<void> {
    groups = (await api<{ items: Group[] }>("/groups")).items;
    const group = groups.find(item => item.id === selectedGroup) || groups[0];
    selectedGroup = group?.id || "";
    if (!group?.courses.some(item => item.id === selectedCourse)) selectedCourse = group?.courses[0]?.id || "";
}

function whenDay(value: string | null): string {
    if (!value) return "дата не назначена";
    return new Date(value + "T00:00:00").toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
}

function showModal(title: string, ...nodes: Node[]): void {
    modalTitle.textContent = title;
    modalBody.replaceChildren(...nodes);
    if (!modal.open) modal.showModal();
}

async function attemptDetails(id: string): Promise<void> {
    const result = await api<Attempt>("/attempts/" + id);
    const nodes: Node[] = [make("p", "", result.score === null ? "Оценка ещё не готова." : "Оценка " + result.score + "/5")];
    for (const [title, points] of [["Засчитано", result.covered], ["Упущено", result.missing], ["Ошибки", result.errors]] as const) {
        if (points?.length) nodes.push(make("h3", "", title), make("ul", "", ...points.map(text => make("li", "", text))));
    }
    if (result.feedback) nodes.push(make("p", "", result.feedback));
    if (result.reference) nodes.push(make("h3", "", "Эталон"), make("p", "reference", result.reference));
    showModal("Последний ответ", ...nodes);
}

function progressView(dashboard: Dashboard, role: Role): HTMLElement {
    const total = dashboard.tickets.length;
    const cards = make("div", "cards");
    const stat = (value: string, text: string) => make("div", "stat", make("strong", "", value), make("span", "", text));
    if (role === "student") {
        const complete = dashboard.tickets.filter(ticket => ticket.passes === 2).length;
        cards.append(stat(complete + " из " + total, "Билеты пройдены дважды"), stat(String(dashboard.streak), "Учебных дней подряд"));
    } else if (dashboard.group) {
        cards.append(stat(dashboard.group.responded + " из " + dashboard.group.active, "Ответили сегодня"),
                     stat(String(dashboard.group.weakTickets.length), "Билеты для повтора"));
    }
    const elements = make("div", "stack", panel("Готовность", cards));
    const grid = make("div", "cards ticket-grid");
    for (const ticket of dashboard.tickets) {
        if (role !== "student") {
            const weak = dashboard.group?.weakTickets.find(item => item.id === ticket.id);
            if (weak) grid.append(make("article", "ticket", make("h3", "", ticket.question),
                                      make("p", "muted", "Средний балл " + weak.mean + "/5 · ответов " + weak.count)));
            continue;
        }
        const fill = make("span");
        fill.style.width = ticket.passes * 50 + "%";
        const progress = make("div", "progress", fill);
        progress.setAttribute("role", "progressbar");
        progress.setAttribute("aria-label", ticket.question);
        progress.setAttribute("aria-valuemin", "0");
        progress.setAttribute("aria-valuemax", "2");
        progress.setAttribute("aria-valuenow", String(ticket.passes));
        const card = make("article", "ticket", make("h3", "", ticket.question), progress,
            make("div", "row", make("span", "pill", ticket.passes + " из 2 проходов"),
                 make("small", "", ticket.lastScore === null ? "Оценки пока нет" : ticket.lastScore + "/5")));
        if (ticket.nextReviewOn) card.append(make("p", "muted", "Повтор · " + whenDay(ticket.nextReviewOn)));
        const attempt = ticket.lastAttemptId;
        if (attempt) card.append(action("Посмотреть разбор", () => attemptDetails(attempt), "text-button"));
        grid.append(card);
    }
    elements.append(panel(role === "student" ? "Карта билетов" : "Слабые билеты",
        grid.childElementCount ? grid : make("p", "muted", role === "student" ? "Билеты появятся после настройки курса." : "Сегодня слабых билетов пока нет.")));
    return elements;
}

function scheduleForm(course: Course): HTMLElement {
    const form = make("form");
    const exam = input("date", course.examDate || "");
    const zone = input("text", course.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Novosibirsk");
    const morning = input("time", course.morningAt || "09:00");
    const evening = input("time", course.eveningAt || "20:00");
    for (const control of [exam, zone, morning, evening]) control.required = true;
    const weekdays = course.weekdays.length ? course.weekdays : [1, 2, 3, 4, 5];
    const picks = week.map((name, index) => {
        const control = input("checkbox");
        control.checked = weekdays.includes(index + 1);
        return { control, label: make("label", "check", control, name) };
    });
    form.append(make("div", "schedule", field("Дата экзамена", exam), field("Часовой пояс", zone),
        field("Утренний билет", morning), field("Вечерняя сводка", evening)),
        make("fieldset", "", make("legend", "", "Учебные дни"), make("div", "row", ...picks.map(item => item.label))));
    submit(form, "Сохранить расписание", async () => {
        await api("/courses/" + course.id, { method: "PATCH", body: {
            examDate: exam.value, timezone: zone.value.trim(), morningAt: morning.value, eveningAt: evening.value,
            weekdays: picks.flatMap((item, index) => item.control.checked ? [index + 1] : []),
        } });
        await render();
        notice("Расписание сохранено.");
    });
    const hint = make("p", "warning section-note");
    hint.hidden = true;
    const control = make("div", "row actions");
    if (course.status === "draft") {
        const activationKey = crypto.randomUUID();
        const acknowledgedActivationKey = crypto.randomUUID();
        const activate = (acknowledgeLoad: boolean) => api("/courses/" + course.id + "/activate", {
            method: "POST", body: { acknowledgeLoad }, key: acknowledgeLoad ? acknowledgedActivationKey : activationKey,
        });
        control.append(action("Начать тренировку", async () => {
            try { await activate(false); }
            catch (error) {
                if (!(error instanceof RequestError) || error.detail.code !== "LOAD_ACK_REQUIRED") throw error;
                hint.textContent = "Дневная цель " + error.detail.dailyTarget + " · может не хватить " + error.detail.projectedMissing + " проходов.";
                hint.hidden = false;
                if (!control.querySelector(".ack")) control.append(action("Запустить с этим расписанием", async () => {
                    await activate(true); await sync();
                }, "primary ack"));
                return;
            }
            await sync();
        }, "primary"));
    } else if (course.status !== "closed") {
        const operation = course.status === "paused" ? "resume" : "pause";
        control.append(action(operation === "resume" ? "Возобновить" : "Поставить на паузу", async () => {
            await api("/courses/" + course.id + "/" + operation, { method: "POST" }); await sync();
        }));
    }
    return panel("Расписание", form, control, hint);
}

function uploadForm(courseId: string, changed: (importId: string) => Promise<void>): HTMLFormElement {
    const form = make("form");
    const text = textarea("");
    text.placeholder = "По одному вопросу на строку";
    const questions = input("file");
    questions.accept = allowed;
    const materials = input("file");
    materials.accept = allowed; materials.multiple = true;
    form.append(field("Текст вопросов", text), field("Или файл с вопросами", questions),
                field("Материалы курса · до 5 файлов", materials));
    let key = crypto.randomUUID();
    form.oninput = () => { key = crypto.randomUUID(); };
    submit(form, "Загрузить вопросы", async () => {
        if (Boolean(questions.files?.length) === Boolean(text.value.trim())) throw new Error("Выберите файл или вставьте текст вопросов.");
        const files = Array.from(materials.files || []);
        if (files.length > 5) throw new Error("Можно добавить до пяти файлов материалов.");
        const data = new FormData();
        if (questions.files?.length) data.append("questionFile", questions.files[0]);
        else data.append("questionText", text.value.trim());
        for (const file of files) data.append("files[]", file);
        const result = await api<{ importId: string }>("/courses/" + courseId + "/imports", { method: "POST", body: data, key });
        key = crypto.randomUUID();
        await changed(result.importId);
    });
    return form;
}

function questionEditor(result: ImportView, done: () => Promise<void>): HTMLFormElement {
    const values = result.questions.map(row => row.text);
    const form = make("form");
    const rows = make("div", "stack");
    let key = crypto.randomUUID();
    const update = () => {
        rows.replaceChildren();
        values.forEach((value, index) => {
            const edit = textarea(value, 2);
            edit.maxLength = 500;
            edit.required = true;
            edit.oninput = () => { values[index] = edit.value; key = crypto.randomUUID(); };
            const remove = button("Убрать вопрос", "text-button");
            remove.onclick = () => { values.splice(index, 1); key = crypto.randomUUID(); update(); };
            rows.append(make("div", "question-edit", field("Вопрос " + (index + 1), edit), remove));
        });
    };
    const add = button("Добавить вопрос");
    add.onclick = () => { if (values.length < 60) { values.push(""); key = crypto.randomUUID(); update(); } };
    form.append(rows, make("div", "row section-note", add));
    update();
    submit(form, "Подтвердить вопросы", async () => {
        await api("/imports/" + result.importId + "/confirm", {
            method: "POST", key, body: { questions: values.map((text, index) => ({ ordinal: index + 1, text })) },
        });
        await done();
    });
    return form;
}

function rubricEditor(ticket: Ticket, refresh: () => Promise<void>): HTMLElement {
    const saved = ticket.rubric;
    const detail = make("details", "", make("summary", "", ticket.question));
    let label = "Ожидает утверждения";
    if (ticket.reviewRequired) label = "Эталон отозван по жалобам · утвердите новую версию";
    else if (ticket.approvedVersion) label = "Утверждена версия " + ticket.approvedVersion;
    detail.append(make("p", "muted", label + (saved?.sourceStatus === "unsourced" ? " · есть пункты без источника" : "")));
    const answer = textarea(saved?.answer || "", 4);
    answer.maxLength = 10000;
    const form = make("form", "", field("Эталонный ответ", answer));
    const pointRows = make("div", "point-list");
    const points = (saved?.points || []).map(point => ({ ...point }));
    const addPoint = () => {
        const id = ["p1", "p2", "p3", "p4", "p5"].find(value => !points.some(point => point.id === value)) || crypto.randomUUID();
        points.push({ id, text: "", citation: null });
    };
    if (!points.length) for (let i = 0; i < 3; i++) addPoint();
    const update = () => {
        pointRows.replaceChildren();
        points.forEach((point, index) => {
            const text = textarea(point.text, 2);
            text.maxLength = 1000;
            const source = make("details", "", make("summary", "", "Цитата"));
            const chunk = input("text", point.citation?.chunkId || "");
            const quote = textarea(point.citation?.quote || "", 2);
            const change = () => { point.citation = chunk.value.trim() || quote.value.trim() ?
                { chunkId: chunk.value.trim(), quote: quote.value } : null; };
            chunk.oninput = change; quote.oninput = change;
            text.oninput = () => { point.text = text.value; };
            source.append(field("ID фрагмента", chunk), field("Дословный текст", quote),
                action("Посмотреть фрагмент", async () => {
                    if (!chunk.value.trim()) throw new Error("Укажите ID фрагмента.");
                    const block = await api<{ filename: string; locator: { page?: number; paragraph?: number }; body: string }>("/chunks/" + encodeURIComponent(chunk.value.trim()));
                    const location = block.locator.page ? "Страница " + block.locator.page :
                        block.locator.paragraph ? "Абзац " + block.locator.paragraph : "";
                    showModal(block.filename, make("p", "muted", location), make("p", "reference", block.body));
                }, "text-button"));
            const remove = button("Убрать пункт", "text-button");
            remove.onclick = () => { points.splice(index, 1); update(); };
            pointRows.append(make("div", "rubric-point", field("Критерий " + (index + 1), text), source, remove));
        });
    };
    const add = button("Добавить пункт");
    add.onclick = () => { if (points.length < 5) { addPoint(); update(); } };
    update();
    form.append(pointRows, make("div", "row section-note", add));
    const consent = input("checkbox");
    form.append(make("label", "check section-note", consent, "Утвердить, даже если у части пунктов нет источника"));
    const save = async (approve: boolean) => {
        await api("/tickets/" + ticket.id + "/rubric", { method: "PUT", body: {
            baseVersion: ticket.version, answer: answer.value, points: points.filter(point => point.text.trim()),
            approve, acknowledgeUnsourced: consent.checked,
        } });
        await refresh();
        notice(approve ? "Эталон утверждён." : "Черновик сохранён.");
    };
    form.append(make("div", "row",
        action("Сохранить правки", () => save(false)),
        action("Утвердить эталон", () => save(true), "primary")));
    detail.append(form);
    return detail;
}

async function setupView(course: Course, group: Group, version: number): Promise<HTMLElement> {
    const view = make("div", "stack");
    if (group.role === "organizer") view.append(scheduleForm(course));
    const ticketBody = make("div", "stack");
    const reload = async () => {
        const result = await api<{ items: Ticket[] }>("/courses/" + course.id + "/tickets");
        ticketBody.replaceChildren(...(result.items.length ? result.items.map(ticket => rubricEditor(ticket, reload)) :
            [make("p", "muted", "Эталоны появятся после подтверждения вопросов.")]));
    };
    if (course.status === "draft") {
        const importBody = make("div", "stack");
        let current = "";
        async function follow(importId: string): Promise<void> {
            current = importId;
            saveStoredImport(course.id, importId);
            while (current === importId && renderToken === version) {
                let result: ImportView;
                try { result = await api<ImportView>("/imports/" + importId); }
                catch (error) {
                    if (renderToken === version && current === importId) importBody.replaceChildren(make("p", "warning", errorText(error)));
                    return;
                }
                if (current !== importId || renderToken !== version) return;
                const warning = result.warnings.map(text => make("p", "warning", text));
                if (result.status === "review") {
                    importBody.replaceChildren(make("h3", "", "Проверьте вопросы"), ...warning, questionEditor(result, async () => follow(importId)));
                    return;
                }
                if (result.status === "ready" || result.status === "failed") {
                    const message = result.error ? errorText(new RequestError(result.error)) :
                        result.status === "ready" ? "Черновики эталонов готовы." : "Импорт завершился ошибкой.";
                    importBody.replaceChildren(make("p", result.status === "failed" ? "warning" : "", message), ...warning);
                    if (result.status === "ready") await reload();
                    return;
                }
                importBody.replaceChildren(make("p", "muted", result.status === "generating" ? "Готовим эталоны…" : "Извлекаем вопросы…"));
                await new Promise(resolve => setTimeout(resolve, 1700));
            }
        }
        const change = async (id: string) => { void follow(id).catch(error => notice(errorText(error), true)); };
        view.append(panel("Материалы", uploadForm(course.id, change), importBody));
        const previous = readStoredImport(course.id);
        if (previous) await change(previous);
    }
    view.append(panel("Эталоны", ticketBody));
    if (group.role === "organizer") {
        const result = await api<{ items: { id: string; alias: string }[] }>("/groups/" + group.id + "/members");
        const people = select(result.items.map(item => ({ id: item.id, label: item.alias })), result.items[0]?.id || "");
        const invitation = make("div", "stack");
        const chosen = action("Создать приглашение", async () => {
            const link = await api<{ startUrl: string }>("/groups/" + group.id + "/teacher-invites",
                { method: "POST", body: { memberId: people.value } });
            const copy = input("url", link.startUrl);
            copy.readOnly = true;
            copy.onfocus = () => copy.select();
            invitation.replaceChildren(field("Ссылка для участника · 24 часа", copy));
        });
        view.append(panel("Преподаватель", result.items.length ?
            make("div", "stack", field("Участник группы", people), chosen, invitation) :
            make("p", "muted", "Пригласить можно после подключения студента к группе.")));
    }
    await reload();
    return view;
}

function createCourseForm(group: Group): HTMLElement {
    const form = make("form");
    const title = input("text");
    title.required = true;
    title.maxLength = 120;
    form.append(field("Название предмета", title));
    submit(form, "Создать курс", async () => {
        const course = await api<Course>("/groups/" + group.id + "/courses", { method: "POST", body: { title: title.value } });
        selectedCourse = course.id;
        tab = "setup";
        await sync();
    });
    return form;
}

async function render(): Promise<void> {
    const version = ++renderToken;
    const group = groups.find(item => item.id === selectedGroup);
    roleLabel.hidden = !group;
    roleLabel.textContent = group ? roles[group.role] : "";
    main.setAttribute("aria-busy", "true");
    if (!group) {
        main.replaceChildren(make("section", "panel empty", make("h1", "", "Подключите группу"),
            make("p", "muted", "Откройте «Ответить» в чате группы. Администратор может начать с /bind.")));
        main.removeAttribute("aria-busy");
        return;
    }

    const editor = group.role !== "student";
    const groupPicker = select(groups.map(item => ({ id: item.id, label: item.title })), selectedGroup);
    groupPicker.onchange = () => {
        selectedGroup = groupPicker.value;
        const target = groups.find(item => item.id === selectedGroup)!;
        selectedCourse = target.courses[0]?.id || "";
        tab = target.courses[0]?.status === "draft" ? "setup" : "progress";
        void render().catch(error => notice(errorText(error), true));
    };
    const coursePicker = select(group.courses.map(item => ({ id: item.id, label: item.title })), selectedCourse);
    const scope = make("div", "scope", field("Группа", groupPicker));
    if (group.courses.length) scope.append(field("Курс", coursePicker));
    coursePicker.onchange = () => {
        selectedCourse = coursePicker.value;
        tab = group.courses.find(item => item.id === selectedCourse)?.status === "draft" ? "setup" : "progress";
        void render().catch(error => notice(errorText(error), true));
    };
    const outlet = make("div", "stack");
    main.replaceChildren(scope, outlet);
    if (!selectedCourse) {
        outlet.append(group.role === "organizer" ? panel("Новый курс", createCourseForm(group)) :
            panel("Курс ещё не запущен", make("p", "muted", "Когда организатор начнёт тренировку, здесь появятся билеты.")));
        main.removeAttribute("aria-busy");
        return;
    }
    const courseId = selectedCourse;
    try {
        const course = await api<Course>("/courses/" + courseId);
        if (version !== renderToken) return;
        outlet.before(make("section", "hero", make("div", "eyebrow", group.title),
            make("div", "row", make("h1", "", course.title), make("span", "pill", labels[course.status])),
            make("p", "", "Экзамен · " + whenDay(course.examDate))));
        if (editor) {
            const tabs = make("nav", "tabs");
            tabs.setAttribute("aria-label", "Разделы курса");
            for (const [type, title] of [["progress", "Прогресс"], ["setup", "Настройка"]] as const) {
                const control = button(title);
                control.setAttribute("aria-selected", String(tab === type));
                control.onclick = () => { tab = type; void render().catch(error => notice(errorText(error), true)); };
                tabs.append(control);
            }
            outlet.before(tabs);
        }
        if (editor && tab === "setup") {
            const settings = await setupView(course, group, version);
            if (version === renderToken) {
                outlet.append(settings);
                if (group.role === "organizer") {
                    outlet.append(make("details", "", make("summary", "", "Новый курс"), createCourseForm(group)));
                }
            }
        } else {
            const dashboard = await api<Dashboard>("/courses/" + courseId + "/dashboard");
            if (version === renderToken) outlet.append(progressView(dashboard, group.role));
        }
    } finally {
        if (version === renderToken) main.removeAttribute("aria-busy");
    }
}

async function sync(): Promise<void> {
    await loadGroups();
    await render();
}

async function mainPage(): Promise<void> {
    const telegram = window.Telegram?.WebApp;
    if (!telegram?.initData) {
        main.replaceChildren(make("section", "panel empty", make("h1", "", "Откройте в Telegram"),
            make("p", "muted", "Зайдите в личный чат «Билет дня», отправьте /app и нажмите «Открыть Билет дня».")));
        main.removeAttribute("aria-busy");
        return;
    }
    telegram.ready();
    telegram.expand();
    const result = await api<{ csrfToken: string }>("/auth/telegram", { method: "POST", body: { initData: telegram.initData } });
    csrf = result.csrfToken;
    await loadGroups();
    const course = groups.find(group => group.id === selectedGroup)?.courses.find(item => item.id === selectedCourse);
    if (course?.status === "draft") tab = "setup";
    await render();
}

void mainPage().catch(error => {
    if (main.getAttribute("aria-busy") === "true") {
        main.replaceChildren(make("section", "panel empty", make("h1", "", "Приложение недоступно"),
            make("p", "muted", "Откройте «Билет дня» повторно из Telegram.")));
        main.removeAttribute("aria-busy");
    }
    notice(errorText(error), true);
});

export {};

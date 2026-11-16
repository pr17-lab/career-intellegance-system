export interface Student {
    id: string;
    user_id: string;
    name: string;
    branch: string;
    semester: number;
    interests?: string;
    created_at: string;
    updated_at: string;
}

export interface StudentListResponse {
    total: number;
    page: number;
    page_size: number;
    students: Student[];
}

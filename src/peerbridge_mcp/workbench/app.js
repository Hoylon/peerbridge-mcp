(() => {
  "use strict";

  const translations = {
    "zh-Hant": {
      access_title: "無法連接本機工作台", access_body: "請由 PeerBridge 桌面程式重新開啟 Modern Workbench。",
      local_workbench: "本機工作台", new_room: "新增房間", chat: "對話", back_to_chat: "返回對話", tasks: "工作板", audit: "審計與證據", all_features: "所有功能",
      recent_rooms: "最近房間", search_rooms: "搜尋房間", search_rooms_placeholder: "輸入房間名稱", clear_search: "清除搜尋", no_room_matches: "沒有符合的房間", operator: "本機操作者", connecting: "連接中", online: "本機已連線", offline: "重新連接中",
      load_older: "載入較早訊息", empty_title: "開始一個協作回合", empty_body: "向房間發送訊息，已連線的 Agent 會按房間規則處理。",
      composer_placeholder: "向房間發送訊息，或輸入 / 選擇工作流程", all_agents: "全體 Agent", normal: "一般", high: "高", critical: "緊急",
      workspace: "工作區", token_usage: "Token 使用量", room_status: "房間狀態", agents: "Agent", round: "回合", work: "工作", seats: "席位",
      messages: "訊息", sent: "已送出", send_failed: "發送失敗", copy: "複製訊息", copied: "已複製", no_operator: "請先加入此房間，才能發送訊息。",
      no_tasks: "目前沒有工作", status: "狀態", claimed_by: "負責者", total: "總計", input: "輸入", output: "輸出", cache: "快取", reasoning: "推理",
      automation_off: "自動：關閉", automation_once: "自動：一輪", automation_discussion: "自動：討論", active: "進行中", waiting: "等待中",
      current_round: "目前協作", connected_agents: "已連線 Agent", task_progress: "工作進度", dispatch_progress: "派送狀態", audit_events: "審計事件", evidence: "證據",
      room_ready: "房間已就緒", discussion_active: "協作討論進行中", one_round_ready: "單輪協作待命", activity_hint: "本機協作狀態與證據已同步", context_active: "同房上下文已啟用",
      evidence_snapshot: "本機快照", completed: "已完成", records: "筆記錄", dispatch_queue: "派送佇列", recent_tasks: "最近工作",
      work_updates: "工作更新", replies: "個回覆", participants: "位參與者", attempt: "嘗試", no_dispatches: "目前沒有派送", no_updates: "目前沒有工作更新",
      dispatch_pending: "待處理", dispatch_claimed: "執行中", dispatch_completed: "已完成", dispatch_failed: "失敗", dispatch_retry: "等待重試", files: "個附件",
      nav_workspace: "工作區", nav_governance: "治理與證據", nav_system: "系統支援", cockpit: "多智能體控制台", review: "互評", change: "變更", trust: "信任與證據", connect: "接入", memory: "記憶", feedback: "意見回饋", announcement: "公告",
      agent_runtime: "Agent 即時狀態", agent_runtime_hint: "以本機 presence、工作階段與派送事件顯示可觀察活動。", model_route: "模型／路由", current_code_diff: "目前程式碼變更", current_code_diff_hint: "只讀取本專案 Git 工作樹；秘密內容會遮蔽，輸出有大小上限。", recorded_changes: "已記錄變更", code_added: "新增", code_deleted: "刪除", diff_unavailable: "目前無法讀取 Git 變更", diff_clean: "工作樹沒有變更", diff_truncated: "只顯示部分差異", observable_activity: "可觀察活動", activity_offline: "離線", activity_idle: "在線待命", activity_running: "正在執行", activity_searching: "正在搜尋網絡", activity_reasoning: "正在思考", activity_reading: "正在讀取", activity_editing: "正在編輯", activity_waiting: "等待回覆", activity_failed: "執行失敗", activity_completed: "已完成", permission: "權限",
      subject_placeholder: "主旨", automation_mode: "自動模式", automation_off_short: "關閉", automation_once_short: "一輪", automation_discussion_short: "討論", max_rounds: "最多回合", max_messages: "最多訊息", stagnation: "停滯回合", apply: "套用", pause: "暫停", resume: "恢復", continue: "繼續", stop: "停止", attach: "附件", refresh: "重新整理", fullscreen: "專注對話", exit_fullscreen: "退出專注對話", native_compact: "壓縮上下文", native_fork: "建立分支", native_review: "原生審查", native_review_hint: "輸入框可選填審查指示；留空則審查未提交變更。", native_actions: "官方工作階段操作",
      managed_sessions: "受管工作階段", session_activity: "工作階段活動", operations: "執行佇列", review_requests: "互評請求", review_results: "互評結果", recent_events: "最近事件", trust_records: "信任記錄", permissions: "權限決策", provider_connections: "供應商連線", routes: "模型路由", memory_records: "記憶記錄", briefings: "工作簡報",
      private_feedback: "私密送出", feedback_ready: "回饋入口已就緒", feedback_unavailable: "回饋設定尚未完成", feedback_body: "一般診斷會遮蔽秘密；完整憑證只會在明確同意後於本機加密。", feedback_summary_label: "問題摘要", feedback_summary_placeholder: "一句話說明問題", feedback_contact_label: "聯絡方式（選填）", feedback_contact_placeholder: "電郵或其他聯絡方式", feedback_message_label: "詳細內容", feedback_message_placeholder: "說明發生了甚麼、預期結果及重現步驟", feedback_attachment_hint: "可附畫面或診斷檔；最多 5 個，共 16 MiB。", feedback_credential_label: "完整 API Key 診斷（選填）", feedback_credential_body: "只有勾選同意後，內容才會在本機以支援公鑰加密；不會以明文送出。", feedback_credential_placeholder: "只在需要重現 Key 解析問題時填寫", feedback_credential_consent: "我明確同意在本機加密後隨本次回饋送出。", feedback_submit: "私密送出", feedback_submitting: "正在封裝及送出…", feedback_delivered: "已安全送達，案件編號", feedback_saved: "未能連線，已在本機保存封裝回饋，案件編號", feedback_required: "請填寫問題摘要及詳細內容。", feedback_consent_required: "填寫完整憑證時必須明確勾選加密同意。", feedback_encryption_unavailable: "此版本未設定支援公鑰，不能附加完整憑證。", show: "顯示", hide: "隱藏", announcement_status: "公告來源狀態", announcement_unconfigured: "尚未設定公告 HTTPS 來源；本機不會偽造公告。", announcement_disabled: "公告網絡同步已停用；目前顯示本機快取。", announcement_ready: "公告 HTTPS 來源已就緒。", announcement_updated: "公告已同步", no_announcements: "目前沒有公告", no_records: "目前沒有記錄", enabled: "已啟用", disabled: "已停用", unknown: "未提供", role: "角色", role_saved: "角色已更新", automation_saved: "自動模式已更新", action_failed: "操作失敗", remove: "移除", attachment_limit: "最多 5 個附件，每個 8 MiB，總計 16 MiB。", period_today: "今天", period_7d: "近 7 日", period_30d: "近 30 日", period_all: "全部",
      agent: "Agent", route: "路由", no_route: "不綁定路由", manage_seats: "管理席位", add_seat: "加入席位", remove_seat: "移除席位", current_member: "現有成員", seat_added: "席位已加入", seat_removed: "席位已移除", no_available_agent: "沒有可加入的 Agent", no_removable_member: "沒有可移除的成員",
      launch_managed_agent: "啟動受管 Agent", managed_agent_hint: "只啟動本機已安裝且受 PeerBridge 允許的官方 CLI。", requested_route: "指定路由（選填）", requested_route_placeholder: "例如官方模型或已設定路由", working_directory: "工作資料夾", initial_instruction: "初始指令（選填）", initial_instruction_placeholder: "啟動後交給 Agent 的第一項工作", start_agent: "啟動 Agent", write_session_confirm: "一般 Agent 可在已批准的治理工作樹內編輯並正常聯網；越界、刪除及高風險權限提升會被拒絕。是否繼續？", full_access_session_confirm: "全權 Agent 只在現在確認一次，之後可使用供應商提供的完整工具及網絡，直至此工作階段停止。這會降低逐項提示保護，只應用於可信任的治理工作樹。是否授權？", session_started: "Agent 工作階段已啟動", session_authorized_once: "本工作階段已一次授權", send_to_session: "向此工作階段發送訊息", managed_input_placeholder: "輸入下一項指令", send: "送出", interrupt: "中斷", managed_attachment_hint: "圖片及音訊會以供應商原生多模態內容送往支援的 runtime；文字檔會經邊界重驗後作受限內嵌。", managed_session_notice: "支援持續會話的官方 runtime 可接收後續訊息與附件；舊式 CLI 會安全退回一次性任務。", persistent_session_notice: "持續工作階段可接收後續訊息與附件。", attachment_delivery: "附件傳遞", transport_accepted: "runtime 已接收", native_content_prepared: "原生多模態內容已準備", native_acp_content_submitted: "ACPX 已接收原生附件", provider_request_completed: "供應商請求已完成", read_path_available: "可用讀取工具開啟", verified_path_available: "已提供驗證路徑", model_view_not_confirmed: "尚未確認模型已檢視", multimodal_input: "多模態輸入", verify_vision: "驗證視覺", vision_test_hint: "送出一張一次性測試圖片，會使用一輪模型呼叫。", semantic_image_verified: "模型已讀懂測試圖片", semantic_image_failed: "模型未通過視覺驗證", semantic_image_runtime_failed: "視覺驗證執行失敗", semantic_image_delivery_failed: "測試圖片未能送達", semantic_image_pending: "視覺驗證中", unsupported_by_agent_runtime: "此 Agent runtime 不支援圖片輸入", vision_verification: "視覺驗證",
      enqueue_workflow: "派發協作工作流程", workflow_hint: "選擇受治理的工作流程，PeerBridge 會建立可追蹤的執行記錄。", workflow: "工作流程", max_attempts: "最多嘗試", timeout_seconds: "逾時秒數", task_description: "工作內容", task_description_placeholder: "說明目標、限制及完成條件", enqueue: "加入佇列", workflow_enqueued: "工作流程已加入佇列", cancel_operation: "取消執行", operation_cancelled: "已要求取消執行", workflow_implement_review: "實作與審查", workflow_investigate_debate: "調查與討論", workflow_read_only_audit: "唯讀審計", workflow_release_gate: "發佈閘門",
      create_room: "建立新房間", create_room_hint: "房間 ID 建立後不可更改；顯示名稱可使用容易辨識的工作主題。", room_id: "房間 ID", room_id_placeholder: "例如 alpha-52-review", room_name: "房間名稱", room_name_placeholder: "例如 Alpha 5.2 發佈檢查", cancel: "取消", create: "建立", room_created: "房間已建立",
      import_history: "匯入 Agent 歷史", import_history_action: "匯入並開啟", history_import_hint: "只解析你明確選取的本機匯出檔；PeerBridge 不會搜尋或上傳其他私人歷史。", history_provider: "來源 Agent", history_generic: "通用 JSON / JSONL", history_file: "對話匯出檔", history_file_limit: "最多 16 MiB；支援 JSON 或 JSONL。", history_contract: "匯入契約", history_contract_id: "保留來源 conversation ID、時間與 SHA-256。", history_contract_redaction: "秘密形狀內容會在寫入前遮蔽。", history_contract_readonly: "匯入房間預設唯讀，不會觸發 Agent 或 fan-out。", history_read_only: "唯讀 Agent 歷史", history_importing: "正在驗證及匯入…", history_imported: "歷史房間已匯入", history_invalid_file: "請選擇 16 MiB 以內的 JSON 或 JSONL 檔案。", imported_room: "匯入歷史",
      governed_worktree: "治理工作樹", governed_worktree_hint: "寫入權限只在已批准的隔離工作樹內生效。", no_governed_worktree: "先在信任與證據頁建立並批准此 Agent 的隔離工作樹。",
      codex_direct_history: "Codex 本機對話", local_conversations: "本機對話", codex_direct_history_hint: "透過官方 app-server 唯讀列出對話；選定前不讀取完整內容。", native_direct_history_hint: "依官方記錄格式唯讀列出本工作區對話；選定前只讀取有限 metadata。", discover_history: "列出對話", history_discovering: "正在讀取對話索引…", history_discovered: "已載入 Agent 對話索引", history_duplicates_collapsed: "已折疊重複內容", no_history_found: "沒有可匯入的對話", history_selection_limit: "每次最多匯入 20 條已勾選對話。", history_selection_required: "請先列出對話並勾選要匯入的項目。", or_import_file: "或選擇匯出檔後列出對話",
      role_equal_participant: "平等參與者", role_researcher: "研究員", role_implementer: "執行者", role_reviewer: "審查員", role_investigator: "調查員", role_planner: "規劃者", role_auditor: "審計員", role_custom: "自訂"
    },
    "zh-Hans": {
      access_title: "无法连接本地工作台", access_body: "请从 PeerBridge 桌面程序重新打开 Modern Workbench。",
      local_workbench: "本地工作台", new_room: "新建房间", chat: "对话", back_to_chat: "返回对话", tasks: "工作板", audit: "审计与证据", all_features: "所有功能",
      recent_rooms: "最近房间", search_rooms: "搜索房间", search_rooms_placeholder: "输入房间名称", clear_search: "清除搜索", no_room_matches: "没有匹配的房间", operator: "本地操作者", connecting: "连接中", online: "本机已连接", offline: "正在重新连接",
      load_older: "加载较早消息", empty_title: "开始一个协作回合", empty_body: "向房间发送消息，已连接的 Agent 会按房间规则处理。",
      composer_placeholder: "向房间发送消息，或输入 / 选择工作流", all_agents: "全部 Agent", normal: "普通", high: "高", critical: "紧急",
      workspace: "工作区", token_usage: "Token 使用量", room_status: "房间状态", agents: "Agent", round: "回合", work: "工作", seats: "席位",
      messages: "消息", sent: "已发送", send_failed: "发送失败", copy: "复制消息", copied: "已复制", no_operator: "请先加入该房间，再发送消息。",
      no_tasks: "当前没有工作", status: "状态", claimed_by: "负责人", total: "总计", input: "输入", output: "输出", cache: "缓存", reasoning: "推理",
      automation_off: "自动：关闭", automation_once: "自动：一轮", automation_discussion: "自动：讨论", active: "进行中", waiting: "等待中",
      current_round: "当前协作", connected_agents: "已连接 Agent", task_progress: "工作进度", dispatch_progress: "派送状态", audit_events: "审计事件", evidence: "证据",
      room_ready: "房间已就绪", discussion_active: "协作讨论进行中", one_round_ready: "单轮协作待命", activity_hint: "本地协作状态与证据已同步", context_active: "同房上下文已启用",
      evidence_snapshot: "本地快照", completed: "已完成", records: "条记录", dispatch_queue: "派送队列", recent_tasks: "最近工作",
      work_updates: "工作更新", replies: "个回复", participants: "位参与者", attempt: "尝试", no_dispatches: "当前没有派送", no_updates: "当前没有工作更新",
      dispatch_pending: "待处理", dispatch_claimed: "执行中", dispatch_completed: "已完成", dispatch_failed: "失败", dispatch_retry: "等待重试", files: "个附件",
      nav_workspace: "工作区", nav_governance: "治理与证据", nav_system: "系统支持", cockpit: "多智能体控制台", review: "互评", change: "变更", trust: "信任与证据", connect: "接入", memory: "记忆", feedback: "意见反馈", announcement: "公告",
      agent_runtime: "Agent 实时状态", agent_runtime_hint: "根据本地 presence、工作阶段和派送事件显示可观察活动。", model_route: "模型／路由", current_code_diff: "当前代码变更", current_code_diff_hint: "只读取本项目 Git 工作树；秘密内容会遮蔽，输出有大小上限。", recorded_changes: "已记录变更", code_added: "新增", code_deleted: "删除", diff_unavailable: "当前无法读取 Git 变更", diff_clean: "工作树没有变更", diff_truncated: "仅显示部分差异", observable_activity: "可观察活动", activity_offline: "离线", activity_idle: "在线待命", activity_running: "正在执行", activity_searching: "正在搜索网络", activity_reasoning: "正在思考", activity_reading: "正在读取", activity_editing: "正在编辑", activity_waiting: "等待回复", activity_failed: "执行失败", activity_completed: "已完成", permission: "权限",
      subject_placeholder: "主题", automation_mode: "自动模式", automation_off_short: "关闭", automation_once_short: "一轮", automation_discussion_short: "讨论", max_rounds: "最多轮次", max_messages: "最多消息", stagnation: "停滞轮次", apply: "应用", pause: "暂停", resume: "恢复", continue: "继续", stop: "停止", attach: "附件", refresh: "刷新", fullscreen: "专注对话", exit_fullscreen: "退出专注对话", native_compact: "压缩上下文", native_fork: "建立分支", native_review: "原生审查", native_review_hint: "输入框可选填审查指示；留空则审查未提交变更。", native_actions: "官方工作阶段操作",
      managed_sessions: "受管工作阶段", session_activity: "工作阶段活动", operations: "执行队列", review_requests: "互评请求", review_results: "互评结果", recent_events: "最近事件", trust_records: "信任记录", permissions: "权限决策", provider_connections: "供应商连接", routes: "模型路由", memory_records: "记忆记录", briefings: "工作简报",
      private_feedback: "私密发送", feedback_ready: "反馈入口已就绪", feedback_unavailable: "反馈设置尚未完成", feedback_body: "一般诊断会遮蔽秘密；完整凭证只会在明确同意后于本机加密。", feedback_summary_label: "问题摘要", feedback_summary_placeholder: "用一句话说明问题", feedback_contact_label: "联系方式（选填）", feedback_contact_placeholder: "邮箱或其他联系方式", feedback_message_label: "详细内容", feedback_message_placeholder: "说明发生了什么、预期结果和复现步骤", feedback_attachment_hint: "可附截图或诊断文件；最多 5 个，共 16 MiB。", feedback_credential_label: "完整 API Key 诊断（选填）", feedback_credential_body: "只有勾选同意后，内容才会在本机使用支持公钥加密；不会以明文发送。", feedback_credential_placeholder: "只在需要复现 Key 解析问题时填写", feedback_credential_consent: "我明确同意在本机加密后随本次反馈发送。", feedback_submit: "私密发送", feedback_submitting: "正在封装并发送…", feedback_delivered: "已安全送达，工单编号", feedback_saved: "无法连接，已在本地保存封装反馈，工单编号", feedback_required: "请填写问题摘要和详细内容。", feedback_consent_required: "填写完整凭证时必须明确勾选加密同意。", feedback_encryption_unavailable: "此版本未设置支持公钥，不能附加完整凭证。", show: "显示", hide: "隐藏", announcement_status: "公告来源状态", announcement_unconfigured: "尚未设置公告 HTTPS 来源；本机不会伪造公告。", announcement_disabled: "公告网络同步已停用；当前显示本地缓存。", announcement_ready: "公告 HTTPS 来源已就绪。", announcement_updated: "公告已同步", no_announcements: "当前没有公告", no_records: "当前没有记录", enabled: "已启用", disabled: "已停用", unknown: "未提供", role: "角色", role_saved: "角色已更新", automation_saved: "自动模式已更新", action_failed: "操作失败", remove: "移除", attachment_limit: "最多 5 个附件，每个 8 MiB，总计 16 MiB。", period_today: "今天", period_7d: "近 7 日", period_30d: "近 30 日", period_all: "全部",
      agent: "Agent", route: "路由", no_route: "不绑定路由", manage_seats: "管理席位", add_seat: "加入席位", remove_seat: "移除席位", current_member: "现有成员", seat_added: "席位已加入", seat_removed: "席位已移除", no_available_agent: "没有可加入的 Agent", no_removable_member: "没有可移除的成员",
      launch_managed_agent: "启动受管 Agent", managed_agent_hint: "只启动本机已安装且受 PeerBridge 允许的官方 CLI。", requested_route: "指定路由（选填）", requested_route_placeholder: "例如官方模型或已设置路由", working_directory: "工作文件夹", initial_instruction: "初始指令（选填）", initial_instruction_placeholder: "启动后交给 Agent 的第一项工作", start_agent: "启动 Agent", write_session_confirm: "普通 Agent 可在已批准的治理工作树中编辑并正常联网；越界、删除和高风险权限提升会被拒绝。是否继续？", full_access_session_confirm: "全权 Agent 只在现在确认一次，之后可使用供应商提供的完整工具和网络，直到此工作阶段停止。这会降低逐项提示保护，只应用于可信的治理工作树。是否授权？", session_started: "Agent 工作阶段已启动", session_authorized_once: "本工作阶段已一次授权", send_to_session: "向此工作阶段发送消息", managed_input_placeholder: "输入下一项指令", send: "发送", interrupt: "中断", managed_attachment_hint: "图片和音频会以供应商原生多模态内容发送到支持的 runtime；文本文件会在边界重验后受限内嵌。", managed_session_notice: "支持持续会话的官方 runtime 可接收后续消息与附件；旧式 CLI 会安全退回一次性任务。", persistent_session_notice: "持续工作阶段可接收后续消息与附件。", attachment_delivery: "附件传递", transport_accepted: "runtime 已接收", native_content_prepared: "原生多模态内容已准备", native_acp_content_submitted: "ACPX 已接收原生附件", provider_request_completed: "供应商请求已完成", read_path_available: "可用读取工具打开", verified_path_available: "已提供验证路径", model_view_not_confirmed: "尚未确认模型已查看", multimodal_input: "多模态输入", verify_vision: "验证视觉", vision_test_hint: "发送一张一次性测试图片，会使用一轮模型调用。", semantic_image_verified: "模型已读懂测试图片", semantic_image_failed: "模型未通过视觉验证", semantic_image_runtime_failed: "视觉验证运行失败", semantic_image_delivery_failed: "测试图片未能送达", semantic_image_pending: "视觉验证中", unsupported_by_agent_runtime: "此 Agent runtime 不支持图片输入", vision_verification: "视觉验证",
      enqueue_workflow: "派发协作工作流", workflow_hint: "选择受治理的工作流，PeerBridge 会建立可追踪的执行记录。", workflow: "工作流", max_attempts: "最多尝试", timeout_seconds: "超时秒数", task_description: "工作内容", task_description_placeholder: "说明目标、限制和完成条件", enqueue: "加入队列", workflow_enqueued: "工作流已加入队列", cancel_operation: "取消执行", operation_cancelled: "已请求取消执行", workflow_implement_review: "实施与审查", workflow_investigate_debate: "调查与讨论", workflow_read_only_audit: "只读审计", workflow_release_gate: "发布闸门",
      create_room: "建立新房间", create_room_hint: "房间 ID 建立后不可更改；显示名称可使用容易识别的工作主题。", room_id: "房间 ID", room_id_placeholder: "例如 alpha-52-review", room_name: "房间名称", room_name_placeholder: "例如 Alpha 5.2 发布检查", cancel: "取消", create: "建立", room_created: "房间已建立",
      import_history: "导入 Agent 历史", import_history_action: "导入并打开", history_import_hint: "只解析你明确选择的本地导出文件；PeerBridge 不会搜索或上传其他私人历史。", history_provider: "来源 Agent", history_generic: "通用 JSON / JSONL", history_file: "对话导出文件", history_file_limit: "最多 16 MiB；支持 JSON 或 JSONL。", history_contract: "导入契约", history_contract_id: "保留来源 conversation ID、时间与 SHA-256。", history_contract_redaction: "秘密形状内容会在写入前遮蔽。", history_contract_readonly: "导入房间默认为只读，不会触发 Agent 或 fan-out。", history_read_only: "只读 Agent 历史", history_importing: "正在验证并导入…", history_imported: "历史房间已导入", history_invalid_file: "请选择 16 MiB 以内的 JSON 或 JSONL 文件。", imported_room: "导入历史",
      governed_worktree: "治理工作树", governed_worktree_hint: "写入权限只在已批准的隔离工作树内生效。", no_governed_worktree: "请先在信任与证据页为此 Agent 建立并批准隔离工作树。",
      codex_direct_history: "Codex 本地对话", local_conversations: "本地对话", codex_direct_history_hint: "通过官方 app-server 只读列出对话；选择前不读取完整内容。", native_direct_history_hint: "按官方记录格式只读列出当前工作区对话；选择前只读取有限元数据。", discover_history: "列出对话", history_discovering: "正在读取对话索引…", history_discovered: "已加载 Agent 对话索引", history_duplicates_collapsed: "已折叠重复内容", no_history_found: "没有可导入的对话", history_selection_limit: "每次最多导入 20 条已勾选对话。", history_selection_required: "请先列出对话并勾选要导入的项目。", or_import_file: "或选择导出文件后列出对话",
      role_equal_participant: "平等参与者", role_researcher: "研究员", role_implementer: "执行者", role_reviewer: "审查员", role_investigator: "调查员", role_planner: "规划者", role_auditor: "审计员", role_custom: "自定义"
    },
    en: {
      access_title: "Local workbench unavailable", access_body: "Open Modern Workbench again from the PeerBridge desktop app.",
      local_workbench: "Local workbench", new_room: "New room", chat: "Chat", back_to_chat: "Back to chat", tasks: "Task board", audit: "Audit & evidence", all_features: "All features",
      recent_rooms: "Recent rooms", search_rooms: "Search rooms", search_rooms_placeholder: "Enter a room name", clear_search: "Clear search", no_room_matches: "No matching rooms", operator: "Local operator", connecting: "Connecting", online: "Local connection", offline: "Reconnecting",
      load_older: "Load earlier messages", empty_title: "Start a collaboration round", empty_body: "Post to the room and connected agents will act under the room policy.",
      composer_placeholder: "Message the room, or type / to select a workflow", all_agents: "All agents", normal: "Normal", high: "High", critical: "Critical",
      workspace: "Workspace", token_usage: "Token usage", room_status: "Room status", agents: "Agents", round: "Round", work: "Work", seats: "Seats",
      messages: "Messages", sent: "Sent", send_failed: "Send failed", copy: "Copy message", copied: "Copied", no_operator: "Join this room before sending a message.",
      no_tasks: "No tasks yet", status: "Status", claimed_by: "Owner", total: "Total", input: "Input", output: "Output", cache: "Cache", reasoning: "Reasoning",
      automation_off: "Automation: off", automation_once: "Automation: once", automation_discussion: "Automation: discussion", active: "Active", waiting: "Waiting",
      current_round: "Current collaboration", connected_agents: "Connected agents", task_progress: "Task progress", dispatch_progress: "Dispatches", audit_events: "Audit events", evidence: "Evidence",
      room_ready: "Room ready", discussion_active: "Collaborative discussion active", one_round_ready: "One-round collaboration ready", activity_hint: "Local coordination state and evidence are synchronized", context_active: "Same-room context enabled",
      evidence_snapshot: "Local snapshot", completed: "Completed", records: "records", dispatch_queue: "Dispatch queue", recent_tasks: "Recent work",
      work_updates: "Work updates", replies: "replies", participants: "participants", attempt: "attempt", no_dispatches: "No dispatches", no_updates: "No work updates",
      dispatch_pending: "Pending", dispatch_claimed: "Running", dispatch_completed: "Completed", dispatch_failed: "Failed", dispatch_retry: "Retry queued", files: "files",
      nav_workspace: "Workspace", nav_governance: "Governance & evidence", nav_system: "System support", cockpit: "Multi-agent cockpit", review: "Peer review", change: "Changes", trust: "Trust & evidence", connect: "Connections", memory: "Memory", feedback: "Feedback", announcement: "Announcements",
      agent_runtime: "Live agent status", agent_runtime_hint: "Shows observable activity from local presence, sessions, and dispatch events.", model_route: "Model / route", current_code_diff: "Current code changes", current_code_diff_hint: "Reads only this project's Git worktree. Secrets are redacted and output is bounded.", recorded_changes: "Recorded changes", code_added: "added", code_deleted: "deleted", diff_unavailable: "Git changes are currently unavailable", diff_clean: "The worktree is clean", diff_truncated: "Only part of the diff is shown", observable_activity: "Observable activity", activity_offline: "Offline", activity_idle: "Online · idle", activity_running: "Running", activity_searching: "Searching the web", activity_reasoning: "Reasoning", activity_reading: "Reading", activity_editing: "Editing", activity_waiting: "Waiting for a reply", activity_failed: "Failed", activity_completed: "Completed", permission: "Permission",
      subject_placeholder: "Subject", automation_mode: "Automation mode", automation_off_short: "Off", automation_once_short: "One round", automation_discussion_short: "Discussion", max_rounds: "Maximum rounds", max_messages: "Maximum messages", stagnation: "Stagnation rounds", apply: "Apply", pause: "Pause", resume: "Resume", continue: "Continue", stop: "Stop", attach: "Attach", refresh: "Refresh", fullscreen: "Focus chat", exit_fullscreen: "Exit focus chat", native_compact: "Compact context", native_fork: "Fork session", native_review: "Native review", native_review_hint: "Optionally use the input field for review instructions; leave it empty to review uncommitted changes.", native_actions: "Official session actions",
      managed_sessions: "Managed sessions", session_activity: "Session activity", operations: "Execution queue", review_requests: "Review requests", review_results: "Review results", recent_events: "Recent events", trust_records: "Trust records", permissions: "Permission decisions", provider_connections: "Provider connections", routes: "Model routes", memory_records: "Memory records", briefings: "Task briefings",
      private_feedback: "Private submission", feedback_ready: "Feedback entry is ready", feedback_unavailable: "Feedback setup is incomplete", feedback_body: "Normal diagnostics redact secrets; complete credentials are locally encrypted only after explicit consent.", feedback_summary_label: "Issue summary", feedback_summary_placeholder: "Describe the issue in one line", feedback_contact_label: "Contact (optional)", feedback_contact_placeholder: "Email or another contact method", feedback_message_label: "Details", feedback_message_placeholder: "Describe what happened, the expected result, and reproduction steps", feedback_attachment_hint: "Attach a screenshot or diagnostic file; up to 5 files and 16 MiB total.", feedback_credential_label: "Complete API key diagnostic (optional)", feedback_credential_body: "Only after explicit consent is the value encrypted locally to the support public key. Plaintext is never sent.", feedback_credential_placeholder: "Use only when reproducing a key parsing problem", feedback_credential_consent: "I explicitly consent to local encryption and inclusion with this submission.", feedback_submit: "Submit privately", feedback_submitting: "Sealing and submitting…", feedback_delivered: "Delivered securely. Case", feedback_saved: "Connection unavailable; sealed feedback saved locally. Case", feedback_required: "Enter an issue summary and details.", feedback_consent_required: "Explicit encryption consent is required when a complete credential is entered.", feedback_encryption_unavailable: "This build has no support public key, so complete credentials cannot be attached.", show: "Show", hide: "Hide", announcement_status: "Announcement source", announcement_unconfigured: "No announcement HTTPS source is configured; local mode will not fabricate announcements.", announcement_disabled: "Announcement network sync is disabled; showing the local cache.", announcement_ready: "The announcement HTTPS source is ready.", announcement_updated: "Announcements synchronized", no_announcements: "No announcements", no_records: "No records yet", enabled: "Enabled", disabled: "Disabled", unknown: "Not reported", role: "Role", role_saved: "Role updated", automation_saved: "Automation updated", action_failed: "Action failed", remove: "Remove", attachment_limit: "Up to 5 attachments, 8 MiB each and 16 MiB total.", period_today: "Today", period_7d: "Last 7 days", period_30d: "Last 30 days", period_all: "All time",
      agent: "Agent", route: "Route", no_route: "No bound route", manage_seats: "Manage seats", add_seat: "Add seat", remove_seat: "Remove seat", current_member: "Current member", seat_added: "Seat added", seat_removed: "Seat removed", no_available_agent: "No agent is available to add", no_removable_member: "No member can be removed",
      launch_managed_agent: "Start a managed agent", managed_agent_hint: "Starts only a locally installed official CLI allowed by PeerBridge.", requested_route: "Requested route (optional)", requested_route_placeholder: "Official model or configured route", working_directory: "Working directory", initial_instruction: "Initial instruction (optional)", initial_instruction_placeholder: "The first task to give the agent after startup", start_agent: "Start agent", write_session_confirm: "Standard Agent mode can edit the approved governed worktree and use normal networking. Out-of-scope, destructive, and high-risk permission escalation is rejected. Continue?", full_access_session_confirm: "Full-access Agent mode asks once now, then enables the complete provider tool set and networking until this session stops. This reduces per-action prompt protection and should be used only for a trusted governed worktree. Authorize?", session_started: "Agent session started", session_authorized_once: "One-time session authorization active", send_to_session: "Send to this session", managed_input_placeholder: "Enter the next instruction", send: "Send", interrupt: "Interrupt", managed_attachment_hint: "Images and audio use provider-native multimodal content when advertised; text files are reverified and embedded within strict limits.", managed_session_notice: "Official runtimes with persistent sessions accept follow-up messages and attachments; legacy CLIs safely fall back to one-shot tasks.", persistent_session_notice: "This persistent session accepts follow-up messages and attachments.", attachment_delivery: "Attachment delivery", transport_accepted: "Accepted by runtime", native_content_prepared: "Native multimodal content prepared", native_acp_content_submitted: "Native attachment accepted by ACPX", provider_request_completed: "Provider request completed", read_path_available: "Available to a read tool", verified_path_available: "Verified path provided", model_view_not_confirmed: "Model inspection not yet confirmed", multimodal_input: "Multimodal input", verify_vision: "Verify vision", vision_test_hint: "Sends a one-use test image and consumes one model turn.", semantic_image_verified: "Model understood the test image", semantic_image_failed: "Model did not pass vision verification", semantic_image_runtime_failed: "Vision verification runtime failed", semantic_image_delivery_failed: "Test image was not delivered", semantic_image_pending: "Vision verification in progress", unsupported_by_agent_runtime: "This Agent runtime does not support image input", vision_verification: "Vision verification",
      enqueue_workflow: "Dispatch a collaboration workflow", workflow_hint: "Choose a governed workflow and PeerBridge will create a traceable operation.", workflow: "Workflow", max_attempts: "Maximum attempts", timeout_seconds: "Timeout in seconds", task_description: "Task", task_description_placeholder: "Describe the objective, constraints, and completion criteria", enqueue: "Enqueue", workflow_enqueued: "Workflow enqueued", cancel_operation: "Cancel operation", operation_cancelled: "Operation cancellation requested", workflow_implement_review: "Implement and review", workflow_investigate_debate: "Investigate and debate", workflow_read_only_audit: "Read-only audit", workflow_release_gate: "Release gate",
      create_room: "Create room", create_room_hint: "A room ID cannot be changed after creation. Use a recognizable work topic as its display name.", room_id: "Room ID", room_id_placeholder: "For example alpha-52-review", room_name: "Room name", room_name_placeholder: "For example Alpha 5.2 release review", cancel: "Cancel", create: "Create", room_created: "Room created",
      import_history: "Import agent history", import_history_action: "Import and open", history_import_hint: "Only the local export you explicitly select is parsed. PeerBridge does not search or upload other private history.", history_provider: "Source agent", history_generic: "Generic JSON / JSONL", history_file: "Conversation export", history_file_limit: "Up to 16 MiB; JSON and JSONL are supported.", history_contract: "Import contract", history_contract_id: "Preserves the source conversation ID, timestamps, and SHA-256.", history_contract_redaction: "Secret-shaped content is redacted before persistence.", history_contract_readonly: "Imported rooms are read-only and never trigger agents or fan-out.", history_read_only: "Read-only agent history", history_importing: "Verifying and importing…", history_imported: "History room imported", history_invalid_file: "Choose a JSON or JSONL file no larger than 16 MiB.", imported_room: "Imported history",
      governed_worktree: "Governed worktree", governed_worktree_hint: "Write authority applies only inside an approved isolated worktree.", no_governed_worktree: "Create and approve an isolated worktree for this agent on Trust and Evidence first.",
      codex_direct_history: "Local Codex conversations", local_conversations: "Local conversations", codex_direct_history_hint: "Lists conversations read-only through the official app-server; full content is not read until selected.", native_direct_history_hint: "Lists workspace conversations read-only using the official record format; only bounded metadata is read before selection.", discover_history: "List conversations", history_discovering: "Reading the conversation index…", history_discovered: "Agent conversation index loaded", history_duplicates_collapsed: "Duplicate content collapsed", no_history_found: "No importable conversations", history_selection_limit: "Import no more than 20 checked conversations at a time.", history_selection_required: "List conversations first, then check the items to import.", or_import_file: "Or choose an export file, then list conversations",
      role_equal_participant: "Equal participant", role_researcher: "Researcher", role_implementer: "Implementer", role_reviewer: "Reviewer", role_investigator: "Investigator", role_planner: "Planner", role_auditor: "Auditor", role_custom: "Custom"
    }
  };

  const capabilityTranslations = {
    "zh-Hant": {
      official_agent_capabilities: "官方 Agent 能力",
      official_agent_capabilities_hint: "依本機安裝、驗證收據及 PeerBridge 映射顯示真實能力。",
      refresh_capabilities: "重新檢查能力",
      capabilities_refreshed: "Agent 能力已更新",
      installed: "已安裝",
      not_installed: "未安裝",
      local_e2e_verified: "本機端對端已驗證",
      no_local_receipt: "尚無本機驗證收據",
      observed_model: "已觀察模型",
      client_version: "客戶端版本",
      permission_tier: "權限層級",
      permission_mode: "權限模式",
      mode_approval_required: "要求核准",
      mode_agent_delegated: "代理核准",
      mode_full_access: "完整存取",
      managed_permission_requires_agent: "一般編輯或完整存取只適用於一個已安裝的本機 Agent；請先選擇單一 Agent。",
      managed_permission_prepared: "草稿已帶到受管 Agent 啟動區；選擇治理工作樹後，只需確認本工作階段一次。",
      managed_permission_unavailable: "這個房間席位沒有可安全啟動的本機官方 Agent。",
      tutorial_title: "快速教學",
      tutorial_short: "開始",
      tutorial_intro: "四步完成第一個可審計的多智能體工作。",
      tutorial_room_title: "建立房間與選擇 Agent",
      tutorial_room_body: "建立房間、加入人工席位，再為每個 Agent 選擇角色、供應商、模型與推理強度。",
      tutorial_send_title: "選擇權限並送出",
      tutorial_send_body: "在輸入框選擇收件者、權限模式、優先級與附件；貼上的圖片會先顯示再送出。",
      tutorial_automation_title: "控制協作回合",
      tutorial_automation_body: "選擇關閉、一輪或有限討論，並設定回合、訊息與停滯上限，避免無限循環。",
      tutorial_evidence_title: "核對工作與證據",
      tutorial_evidence_body: "在控制台查看 Agent 狀態、模型、活動和答案；在變更、互評與審計頁核對程式碼及證據。",
      tutorial_later: "稍後",
      tutorial_done: "知道了",
      approval_waiting: "等待你的核准",
      approval_allow_once: "批准一次",
      approval_allow_session: "本工作階段允許",
      approval_deny: "拒絕",
      approval_risk: "風險",
      adapter_capabilities: "適配器能力",
      adapter_state_supported: "已支援",
      adapter_state_conditional: "條件支援",
      adapter_state_unsupported: "未支援",
      tier_observe: "聊天／只讀",
      tier_review: "審查／只讀",
      tier_edit: "Agent（一般）",
      tier_full_development: "Agent（全權）",
      permission_hint_observe: "只讀工作區，不修改檔案；網路不會因 Agent 編輯權限而啟用。",
      permission_hint_review: "只讀審查與分析，不修改檔案。",
      permission_hint_edit: "可編輯治理工作樹並正常聯網；採用各官方 Agent 的一般模式，拒絕越界與高風險提升。",
      permission_hint_full_development: "開始時確認一次；開啟官方 Agent 完整工具及正常網路，不再逐項提示。",
      status_verified: "已驗證",
      status_configured: "已配置，待端對端驗證",
      status_available_not_mapped: "官方可用，尚未接入",
      status_gated: "需治理授權",
      status_not_verified: "未驗證",
      status_unavailable: "不可用",
      capability_real_inference: "真實模型推理",
      capability_mcp_tools: "MCP 工具呼叫",
      capability_model_selection: "模型選擇",
      capability_reasoning_selection: "推理強度",
      capability_persistent_session: "持續工作階段",
      capability_file_read: "讀取檔案",
      capability_multimodal_input: "多模態輸入",
      capability_file_edit: "編輯檔案",
      capability_shell_tests: "命令與測試",
      capability_diff_review: "差異審查",
      capability_permission_approval: "權限批准",
      capability_session_resume: "恢復工作階段",
      capability_skills: "Skills",
      authorize_agent_identity: "授權 Agent 接入",
      authorize_agent_identity_hint: "建立十分鐘內只可使用一次的權限，供 CLI 簽發綁定工具 profile 的 Agent capability。",
      agent_id: "Agent ID",
      capability_profile: "能力 Profile",
      profile_collaborator: "協作者",
      profile_observer: "觀察者",
      authorize_once: "一次授權",
      permission_decision_id: "權限決策 ID",
      identity_cli_hint: "在十分鐘內把此 ID 加到 identity issue 命令的 --permission-decision-id。",
      identity_authorized: "Agent 接入已授權",
      revoke_agent_identity: "撤銷 Agent 接入",
      revoke_agent_identity_hint: "撤銷後，新舊 stdio 工作階段在下一次工具呼叫都會被永久封鎖。",
      identity_capability_id: "Capability ID",
      identity_revocation_reason: "撤銷原因",
      revoke_identity: "撤銷接入",
      identity_revoked: "Agent 接入已撤銷",
      capability_hooks_plugins: "Hooks／Plugins",
      capability_subagents: "子 Agent",
      capability_progress_events: "即時進度事件",
      capability_peerbridge_audit: "PeerBridge 審計",
      native_client_contract: "官方客戶端契約",
      peerbridge_verified_mapping: "PeerBridge 接入狀態",
      transport: "啟動通道",
      transport_direct_official_cli: "官方 CLI 直接啟動",
      transport_official_cli_via_acpx: "官方 CLI 經 ACPX",
      session_mode: "工作階段模式",
      session_one_shot: "一次性任務", session_persistent: "持續工作階段",
      input_transport: "輸入方式",
      input_transport_official_persistent_protocol: "官方持續協議",
      input_transport_acpx_named_session: "ACPX 命名持續會話",
      input_stdin_once: "stdin 單次輸入",
      read_only_profile: "唯讀配置",
      model_route_configurable: "可指定模型路由",
      session_resume_mapped: "持續／恢復接入",
      mapped_yes: "已接入",
      mapped_no: "未接入",
      one_shot_notice: "此官方 CLI 工作階段只接受一次輸入；完成後請啟動新任務。",
      one_shot_input_used: "初始工作已送出；此一次性任務不接受第二次輸入。",
      session_action_completed: "工作階段操作完成",
      token_breakdown: "Token 分拆", token_trend: "Token 趨勢", provider_usage: "供應商使用量", model_usage: "模型使用量"
    },
    "zh-Hans": {
      official_agent_capabilities: "官方 Agent 能力",
      official_agent_capabilities_hint: "根据本机安装、验证收据和 PeerBridge 映射显示真实能力。",
      refresh_capabilities: "重新检查能力",
      capabilities_refreshed: "Agent 能力已更新",
      installed: "已安装",
      not_installed: "未安装",
      local_e2e_verified: "本机端到端已验证",
      no_local_receipt: "暂无本机验证收据",
      observed_model: "已观察模型",
      client_version: "客户端版本",
      permission_tier: "权限层级",
      permission_mode: "权限模式",
      mode_approval_required: "要求批准",
      mode_agent_delegated: "代理批准",
      mode_full_access: "完整访问",
      managed_permission_requires_agent: "普通编辑或完整访问只适用于一个已安装的本地 Agent；请先选择单个 Agent。",
      managed_permission_prepared: "草稿已带到受管 Agent 启动区；选择治理工作树后，只需为本工作阶段确认一次。",
      managed_permission_unavailable: "该房间席位没有可安全启动的本地官方 Agent。",
      tutorial_title: "快速教程",
      tutorial_short: "开始",
      tutorial_intro: "四步完成第一个可审计的多智能体工作。",
      tutorial_room_title: "创建房间并选择 Agent",
      tutorial_room_body: "创建房间、加入人工席位，再为每个 Agent 选择角色、供应商、模型和推理强度。",
      tutorial_send_title: "选择权限并发送",
      tutorial_send_body: "在输入框选择收件人、权限模式、优先级和附件；粘贴的图片会先显示再发送。",
      tutorial_automation_title: "控制协作轮次",
      tutorial_automation_body: "选择关闭、一轮或有限讨论，并设置轮次、消息和停滞上限，避免无限循环。",
      tutorial_evidence_title: "核对工作与证据",
      tutorial_evidence_body: "在控制台查看 Agent 状态、模型、活动和回答；在变更、互评和审计页核对代码及证据。",
      tutorial_later: "稍后",
      tutorial_done: "知道了",
      approval_waiting: "等待你的批准",
      approval_allow_once: "批准一次",
      approval_allow_session: "本工作阶段允许",
      approval_deny: "拒绝",
      approval_risk: "风险",
      adapter_capabilities: "适配器能力",
      adapter_state_supported: "已支持",
      adapter_state_conditional: "条件支持",
      adapter_state_unsupported: "未支持",
      tier_observe: "聊天／只读",
      tier_review: "审查／只读",
      tier_edit: "Agent（普通）",
      tier_full_development: "Agent（全权）",
      permission_hint_observe: "只读工作区，不修改文件；网络不会因 Agent 编辑权限而启用。",
      permission_hint_review: "只读审查与分析，不修改文件。",
      permission_hint_edit: "可编辑治理工作树并正常联网；采用各官方 Agent 的普通模式，拒绝越界和高风险提升。",
      permission_hint_full_development: "开始时确认一次；开启官方 Agent 完整工具和正常网络，不再逐项提示。",
      status_verified: "已验证",
      status_configured: "已配置，待端到端验证",
      status_available_not_mapped: "官方可用，尚未接入",
      status_gated: "需要治理授权",
      status_not_verified: "未验证",
      status_unavailable: "不可用",
      capability_real_inference: "真实模型推理",
      capability_mcp_tools: "MCP 工具调用",
      capability_model_selection: "模型选择",
      capability_reasoning_selection: "推理强度",
      capability_persistent_session: "持续工作阶段",
      capability_file_read: "读取文件",
      capability_multimodal_input: "多模态输入",
      capability_file_edit: "编辑文件",
      capability_shell_tests: "命令与测试",
      capability_diff_review: "差异审查",
      capability_permission_approval: "权限批准",
      capability_session_resume: "恢复工作阶段",
      capability_skills: "Skills",
      authorize_agent_identity: "授权 Agent 接入",
      authorize_agent_identity_hint: "建立十分钟内只能使用一次的权限，供 CLI 签发绑定工具 profile 的 Agent capability。",
      agent_id: "Agent ID",
      capability_profile: "能力 Profile",
      profile_collaborator: "协作者",
      profile_observer: "观察者",
      authorize_once: "一次授权",
      permission_decision_id: "权限决策 ID",
      identity_cli_hint: "请在十分钟内把此 ID 加到 identity issue 命令的 --permission-decision-id。",
      identity_authorized: "Agent 接入已授权",
      revoke_agent_identity: "撤销 Agent 接入",
      revoke_agent_identity_hint: "撤销后，新旧 stdio 工作阶段在下一次工具调用都会被永久阻止。",
      identity_capability_id: "Capability ID",
      identity_revocation_reason: "撤销原因",
      revoke_identity: "撤销接入",
      identity_revoked: "Agent 接入已撤销",
      capability_hooks_plugins: "Hooks／Plugins",
      capability_subagents: "子 Agent",
      capability_progress_events: "实时进度事件",
      capability_peerbridge_audit: "PeerBridge 审计",
      native_client_contract: "官方客户端契约",
      peerbridge_verified_mapping: "PeerBridge 接入状态",
      transport: "启动通道",
      transport_direct_official_cli: "官方 CLI 直接启动",
      transport_official_cli_via_acpx: "官方 CLI 经 ACPX",
      session_mode: "工作阶段模式",
      session_one_shot: "一次性任务", session_persistent: "持续工作阶段",
      input_transport: "输入方式",
      input_transport_official_persistent_protocol: "官方持续协议",
      input_transport_acpx_named_session: "ACPX 命名持续会话",
      input_stdin_once: "stdin 单次输入",
      read_only_profile: "只读配置",
      model_route_configurable: "可指定模型路由",
      session_resume_mapped: "持续／恢复接入",
      mapped_yes: "已接入",
      mapped_no: "未接入",
      one_shot_notice: "此官方 CLI 工作阶段只接受一次输入；完成后请启动新任务。",
      one_shot_input_used: "初始工作已发送；此一次性任务不接受第二次输入。",
      session_action_completed: "工作阶段操作完成",
      token_breakdown: "Token 拆分", token_trend: "Token 趋势", provider_usage: "供应商使用量", model_usage: "模型使用量"
    },
    en: {
      official_agent_capabilities: "Official agent capabilities",
      official_agent_capabilities_hint: "Shows actual local installation, verification receipts, and PeerBridge mappings.",
      refresh_capabilities: "Recheck capabilities",
      capabilities_refreshed: "Agent capabilities refreshed",
      installed: "Installed",
      not_installed: "Not installed",
      local_e2e_verified: "Local E2E verified",
      no_local_receipt: "No local verification receipt",
      observed_model: "Observed model",
      client_version: "Client version",
      permission_tier: "Permission tier",
      permission_mode: "Permission mode",
      mode_approval_required: "Ask for approval",
      mode_agent_delegated: "Delegate approval",
      mode_full_access: "Full access",
      managed_permission_requires_agent: "Standard or full access requires one installed local Agent. Select a single Agent first.",
      managed_permission_prepared: "The draft is ready in Managed Agent. Choose a governed worktree and authorize this session once.",
      managed_permission_unavailable: "This room seat has no local official Agent that can be started safely.",
      tutorial_title: "Quick start",
      tutorial_short: "Start",
      tutorial_intro: "Complete your first auditable multi-agent task in four steps.",
      tutorial_room_title: "Create a room and choose Agents",
      tutorial_room_body: "Create a room, join as the human operator, then choose each Agent's role, provider, model, and reasoning effort.",
      tutorial_send_title: "Choose permission and send",
      tutorial_send_body: "Choose recipient, permission mode, priority, and attachments in the composer. Pasted images appear before sending.",
      tutorial_automation_title: "Control collaboration rounds",
      tutorial_automation_body: "Choose Off, One round, or bounded Discussion, then set round, message, and stagnation limits.",
      tutorial_evidence_title: "Verify work and evidence",
      tutorial_evidence_body: "Use Cockpit for Agent state, model, activity, and answers. Use Changes, Reviews, and Audit for code and evidence.",
      tutorial_later: "Later",
      tutorial_done: "Got it",
      approval_waiting: "Waiting for your approval",
      approval_allow_once: "Allow once",
      approval_allow_session: "Allow for session",
      approval_deny: "Deny",
      approval_risk: "Risk",
      adapter_capabilities: "Adapter capabilities",
      adapter_state_supported: "Supported",
      adapter_state_conditional: "Conditional",
      adapter_state_unsupported: "Unsupported",
      tier_observe: "Chat / read only",
      tier_review: "Review / read only",
      tier_edit: "Agent (standard)",
      tier_full_development: "Agent (full access)",
      permission_hint_observe: "Read the workspace without modifying files. Agent edit networking is not enabled in this mode.",
      permission_hint_review: "Review and analyze read-only without modifying files.",
      permission_hint_edit: "Edit the governed worktree with normal networking under each official Agent's standard policy; out-of-scope and high-risk escalation is rejected.",
      permission_hint_full_development: "Confirm once at startup to enable the official Agent's complete tools and normal networking without repeated prompts.",
      status_verified: "Verified",
      status_configured: "Configured; E2E pending",
      status_available_not_mapped: "Available upstream, not mapped",
      status_gated: "Governance approval required",
      status_not_verified: "Not verified",
      status_unavailable: "Unavailable",
      capability_real_inference: "Real model inference",
      capability_mcp_tools: "MCP tool invocation",
      capability_model_selection: "Model selection",
      capability_reasoning_selection: "Reasoning effort",
      capability_persistent_session: "Persistent session",
      capability_file_read: "File reading",
      capability_multimodal_input: "Multimodal input",
      capability_file_edit: "File editing",
      capability_shell_tests: "Commands and tests",
      capability_diff_review: "Diff review",
      capability_permission_approval: "Permission approval",
      capability_session_resume: "Session resume",
      capability_skills: "Skills",
      authorize_agent_identity: "Authorize agent connection",
      authorize_agent_identity_hint: "Create a ten-minute, single-use permission for the CLI to issue an Agent capability bound to a fixed tool profile.",
      agent_id: "Agent ID",
      capability_profile: "Capability profile",
      profile_collaborator: "Collaborator",
      profile_observer: "Observer",
      authorize_once: "Authorize once",
      permission_decision_id: "Permission decision ID",
      identity_cli_hint: "Within ten minutes, add this ID to identity issue with --permission-decision-id.",
      identity_authorized: "Agent connection authorized",
      revoke_agent_identity: "Revoke agent connection",
      revoke_agent_identity_hint: "After revocation, new and existing stdio sessions are permanently fenced on their next tool call.",
      identity_capability_id: "Capability ID",
      identity_revocation_reason: "Revocation reason",
      revoke_identity: "Revoke access",
      identity_revoked: "Agent connection revoked",
      capability_hooks_plugins: "Hooks / plugins",
      capability_subagents: "Subagents",
      capability_progress_events: "Live progress events",
      capability_peerbridge_audit: "PeerBridge audit",
      native_client_contract: "Official client contract",
      peerbridge_verified_mapping: "PeerBridge integration status",
      transport: "Launch transport",
      transport_direct_official_cli: "Direct official CLI",
      transport_official_cli_via_acpx: "Official CLI through ACPX",
      session_mode: "Session mode",
      session_one_shot: "One-shot task", session_persistent: "Persistent session",
      input_transport: "Input transport",
      input_transport_official_persistent_protocol: "Official persistent protocol",
      input_transport_acpx_named_session: "ACPX named persistent session",
      input_stdin_once: "Single stdin submission",
      read_only_profile: "Read-only profile",
      model_route_configurable: "Model route selectable",
      session_resume_mapped: "Persistent / resume mapping",
      mapped_yes: "Mapped",
      mapped_no: "Not mapped",
      one_shot_notice: "This official CLI session accepts one input. Start a new task after it finishes.",
      one_shot_input_used: "Initial work was submitted; this one-shot task cannot accept another input.",
      session_action_completed: "Session action completed",
      token_breakdown: "Token breakdown", token_trend: "Token trend", provider_usage: "Provider usage", model_usage: "Model usage"
    }
  };

  const adapterCapabilityLabels = {
    "zh-Hant": {
      "model-selection": "模型選擇", "reasoning-selection": "推理強度", "persistent-session": "持續工作階段",
      "session-resume": "恢復工作階段", "session-fork": "建立分支", "session-compact": "壓縮上下文",
      "native-review": "原生審查", "image-input": "圖片輸入", "observable-events": "即時活動事件",
      "token-usage": "Token 使用量", "interactive-approval": "互動核准", "history-import": "歷史匯入"
    },
    "zh-Hans": {
      "model-selection": "模型选择", "reasoning-selection": "推理强度", "persistent-session": "持续工作阶段",
      "session-resume": "恢复工作阶段", "session-fork": "创建分支", "session-compact": "压缩上下文",
      "native-review": "原生审查", "image-input": "图片输入", "observable-events": "实时活动事件",
      "token-usage": "Token 使用量", "interactive-approval": "互动批准", "history-import": "历史导入"
    },
    en: {
      "model-selection": "Model selection", "reasoning-selection": "Reasoning effort", "persistent-session": "Persistent session",
      "session-resume": "Resume session", "session-fork": "Fork session", "session-compact": "Compact context",
      "native-review": "Native review", "image-input": "Image input", "observable-events": "Live activity events",
      "token-usage": "Token usage", "interactive-approval": "Interactive approval", "history-import": "History import"
    }
  };

  const controlTranslations = {
    "zh-Hant": {
      session_layout: "工作階段檢視", cockpit_grid: "網格", cockpit_focus: "專注", cockpit_timeline: "時間軸",
      session_details: "工作階段詳情", no_session_selected: "尚未選擇工作階段", terminal: "終端", activity: "活動", final_answer: "最終回答",
      session_contract: "工作階段契約", latest_events: "最新事件", no_final_answer: "尚未收到最終回答", execution_receipt: "執行收據",
      schedules: "本機排程", schedule_hint: "排程只會加入既有受治理佇列，保留權限、逾時及停止控制。", schedule_id: "排程 ID",
      interval_minutes: "間隔分鐘", start_delay_minutes: "首次延遲分鐘", permission_decision_id_optional: "權限決策 ID（選填）",
      save_schedule: "儲存排程", saved_schedules: "已儲存排程", schedule_saved: "排程已儲存", schedule_enabled: "排程已啟用", schedule_disabled: "排程已停用",
      capability_governance: "能力授權", capability_governance_hint: "註冊具 SHA 綁定的 Skill 或 MCP 工具，再由操作者逐一允許或拒絕。",
      register_capability: "註冊能力", grant_capability: "授權能力", capability_id: "能力 ID", registry_version: "版本", capability_kind: "類型",
      display_name: "顯示名稱", sensitivity: "敏感度", read_only: "唯讀", write_access: "寫入", sensitive_access: "敏感", register: "註冊",
      principal_type: "對象類型", principal_id: "對象 ID", room: "房間", decision: "決策", allow: "允許", deny: "拒絕", reason: "原因",
      save_decision: "儲存決策", registered_capabilities: "已註冊能力", capability_grants: "能力授權記錄", capability_registered: "能力已註冊", grant_saved: "能力決策已儲存",
      execution_governance: "隔離執行", execution_governance_hint: "先建立限時人類權限，再建立獨立 Git worktree；PeerBridge 不會自動合併變更。",
      permission_decision: "權限決策", decision_id: "決策 ID", task_id: "工作 ID", ttl_hours: "有效小時", permission_decision_id: "權限決策 ID",
      create_isolated_execution: "建立隔離執行", binding_id: "綁定 ID", create_execution: "建立執行", execution_bindings: "執行綁定",
      permission_saved: "權限決策已儲存", execution_created: "隔離執行已建立", seal: "封存", verify: "驗證", execution_sealed: "執行已封存", execution_verified: "來源驗證完成",
      proof_bundle: "證據包", proof_bundle_hint: "輸出經清理的可攜式證據，或對既有證據包進行零寫入驗證。", export_proof: "匯出證據包",
      bundle_path: "證據包路徑", verify_proof: "驗證證據包", proof_exported: "證據包已匯出", proof_verified: "證據包驗證通過",
      sha_binding: "SHA 綁定", working_tree: "隔離工作樹", permission_lifetime: "權限有效期", enable: "啟用", disable: "停用"
    },
    "zh-Hans": {
      session_layout: "工作阶段视图", cockpit_grid: "网格", cockpit_focus: "专注", cockpit_timeline: "时间轴",
      session_details: "工作阶段详情", no_session_selected: "尚未选择工作阶段", terminal: "终端", activity: "活动", final_answer: "最终回答",
      session_contract: "工作阶段契约", latest_events: "最新事件", no_final_answer: "尚未收到最终回答", execution_receipt: "执行收据",
      schedules: "本地计划", schedule_hint: "计划只会加入现有受治理队列，并保留权限、超时和停止控制。", schedule_id: "计划 ID",
      interval_minutes: "间隔分钟", start_delay_minutes: "首次延迟分钟", permission_decision_id_optional: "权限决策 ID（选填）",
      save_schedule: "保存计划", saved_schedules: "已保存计划", schedule_saved: "计划已保存", schedule_enabled: "计划已启用", schedule_disabled: "计划已停用",
      capability_governance: "能力授权", capability_governance_hint: "注册带 SHA 绑定的 Skill 或 MCP 工具，再由操作者逐项允许或拒绝。",
      register_capability: "注册能力", grant_capability: "授权能力", capability_id: "能力 ID", registry_version: "版本", capability_kind: "类型",
      display_name: "显示名称", sensitivity: "敏感度", read_only: "只读", write_access: "写入", sensitive_access: "敏感", register: "注册",
      principal_type: "对象类型", principal_id: "对象 ID", room: "房间", decision: "决策", allow: "允许", deny: "拒绝", reason: "原因",
      save_decision: "保存决策", registered_capabilities: "已注册能力", capability_grants: "能力授权记录", capability_registered: "能力已注册", grant_saved: "能力决策已保存",
      execution_governance: "隔离执行", execution_governance_hint: "先建立限时人工权限，再建立独立 Git worktree；PeerBridge 不会自动合并变更。",
      permission_decision: "权限决策", decision_id: "决策 ID", task_id: "工作 ID", ttl_hours: "有效小时", permission_decision_id: "权限决策 ID",
      create_isolated_execution: "建立隔离执行", binding_id: "绑定 ID", create_execution: "建立执行", execution_bindings: "执行绑定",
      permission_saved: "权限决策已保存", execution_created: "隔离执行已建立", seal: "封存", verify: "验证", execution_sealed: "执行已封存", execution_verified: "来源验证完成",
      proof_bundle: "证据包", proof_bundle_hint: "导出已清理的便携证据，或对现有证据包进行零写入验证。", export_proof: "导出证据包",
      bundle_path: "证据包路径", verify_proof: "验证证据包", proof_exported: "证据包已导出", proof_verified: "证据包验证通过",
      sha_binding: "SHA 绑定", working_tree: "隔离工作树", permission_lifetime: "权限有效期", enable: "启用", disable: "停用"
    },
    en: {
      session_layout: "Session layout", cockpit_grid: "Grid", cockpit_focus: "Focus", cockpit_timeline: "Timeline",
      session_details: "Session details", no_session_selected: "No session selected", terminal: "Terminal", activity: "Activity", final_answer: "Final answer",
      session_contract: "Session contract", latest_events: "Latest events", no_final_answer: "No final answer received", execution_receipt: "Execution receipt",
      schedules: "Local schedules", schedule_hint: "Schedules enqueue existing governed workflows and retain permission, timeout, and stop controls.", schedule_id: "Schedule ID",
      interval_minutes: "Interval minutes", start_delay_minutes: "Initial delay minutes", permission_decision_id_optional: "Permission decision ID (optional)",
      save_schedule: "Save schedule", saved_schedules: "Saved schedules", schedule_saved: "Schedule saved", schedule_enabled: "Schedule enabled", schedule_disabled: "Schedule disabled",
      capability_governance: "Capability governance", capability_governance_hint: "Register SHA-bound Skills or MCP tools, then explicitly allow or deny each principal.",
      register_capability: "Register capability", grant_capability: "Grant capability", capability_id: "Capability ID", registry_version: "Version", capability_kind: "Kind",
      display_name: "Display name", sensitivity: "Sensitivity", read_only: "Read only", write_access: "Write", sensitive_access: "Sensitive", register: "Register",
      principal_type: "Principal type", principal_id: "Principal ID", room: "Room", decision: "Decision", allow: "Allow", deny: "Deny", reason: "Reason",
      save_decision: "Save decision", registered_capabilities: "Registered capabilities", capability_grants: "Capability grants", capability_registered: "Capability registered", grant_saved: "Capability decision saved",
      execution_governance: "Isolated execution", execution_governance_hint: "Create a time-limited human permission, then an isolated Git worktree. PeerBridge never merges automatically.",
      permission_decision: "Permission decision", decision_id: "Decision ID", task_id: "Task ID", ttl_hours: "Valid hours", permission_decision_id: "Permission decision ID",
      create_isolated_execution: "Create isolated execution", binding_id: "Binding ID", create_execution: "Create execution", execution_bindings: "Execution bindings",
      permission_saved: "Permission decision saved", execution_created: "Isolated execution created", seal: "Seal", verify: "Verify", execution_sealed: "Execution sealed", execution_verified: "Source verification complete",
      proof_bundle: "Proof bundle", proof_bundle_hint: "Export a sanitized portable proof bundle or verify an existing bundle with zero writes.", export_proof: "Export proof",
      bundle_path: "Bundle path", verify_proof: "Verify proof", proof_exported: "Proof bundle exported", proof_verified: "Proof bundle verified",
      sha_binding: "SHA binding", working_tree: "Isolated worktree", permission_lifetime: "Permission lifetime", enable: "Enable", disable: "Disable"
    }
  };

  const integrationTranslations = {
    "zh-Hant": {
      configure_agent: "設定 Agent", view_terminal: "查看終端", code_files: "個檔案", models_truncated: "模型清單已達顯示上限", activity_stale: "連線已過期", appearance: "外觀", choose_appearance: "選擇外觀", choose_appearance_hint: "選擇 Pixel Control Room 或 Modern Workbench；重新啟動後套用。", pixel_appearance_hint: "原本深色像素風、高密度終端與控制介面。", modern_appearance_hint: "對話優先，整合模型、權限、變更與證據。", save_appearance: "儲存外觀", appearance_saved: "外觀已儲存，重新啟動後套用",
      ccswitch_title: "CC Switch 一鍵接入", ccswitch_hint: "讀取 CC Switch 已保存的供應商與模型；只有按下確認後才會切換目前供應商。", ccswitch_app: "應用程式", ccswitch_provider: "CC Switch 供應商",
      discover_first: "先讀取供應商", discover_models_first: "先讀取模型", discover_providers: "讀取供應商", discover_models: "讀取模型", save_route: "建立路由", activate_provider: "切換供應商",
      provider_setup: "直接 API／中轉／本機供應商", provider_setup_hint: "憑證只保存於本機安全儲存；PeerBridge 資料庫只記錄不可逆指紋與端點雜湊。", connection_id: "連線 ID", route_class: "路由類型", endpoint: "API 端點", official: "官方", relay: "中轉", local: "本機", save_provider: "保存供應商",
      model_route_setup: "讀取模型並建立路由", model_route_setup_hint: "模型清單由所選供應商即時回報，不會用預設名稱冒充。", provider_saved: "供應商已保存", providers_discovered: "供應商清單已更新", models_discovered: "模型清單已更新", route_saved: "模型路由已建立", provider_activated: "CC Switch 供應商已切換",
      install_agent: "安裝", update_agent: "更新", install_agent_confirm: "PeerBridge 將啟動供應商的官方安裝程序。是否繼續？", agent_installer_started: "官方 Agent 安裝程序已啟動", clipboard_image_attached: "已從剪貼簿加入圖片", terminal_not_started: "終端尚未啟動", start_terminal: "啟動終端", model: "模型", reasoning_mode: "推理強度", publisher_guide: "官方安裝指南", attachment_only_message: "附件", all_session_timeline: "全部終端時間軸", attachment_type_invalid: "附件格式不受支援", attachment_count_limit: "附件最多 5 個", attachment_file_size_limit: "每個附件不可超過 8 MiB", attachment_total_size_limit: "附件總大小不可超過 16 MiB", verify_audit_chain: "驗證審計鏈", audit_chain_verified: "審計鏈驗證通過", mark_all_read: "全部標為已讀", announcements_marked_read: "公告已標為已讀", usage_truncated: "所選期間的趨勢資料已截斷", schedule_next_run: "下次執行", expires: "到期", consumed: "已使用", ccswitch_missing_endpoint: "此供應商未設定模型 API 端點", confirm_ccswitch_switch: "確認把 {app} 切換到 {provider}？", bootstrap_failed: "本機資料載入失敗；可按重新整理重試", acpx_runtime: "ACPX 執行環境", acpx_required: "Grok 與 Kimi 的持續工作階段需要 ACPX", install_dependency: "安裝依賴", dispatches: "派送", events: "事件", memories: "記憶", reason_low: "低", reason_medium: "中", reason_high: "高", reason_xhigh: "特高", reason_max: "最高", proof_required: "請先填寫必要的工作 ID 或證據包路徑", edit_schedule: "編輯排程", start_review_workflow: "派發互評流程", continue_history: "從此歷史繼續", history_continued: "已建立可寫續談房間"
    },
    "zh-Hans": {
      configure_agent: "设置 Agent", view_terminal: "查看终端", code_files: "个文件", models_truncated: "模型列表已达到显示上限", activity_stale: "连接已过期", appearance: "外观", choose_appearance: "选择外观", choose_appearance_hint: "选择 Pixel Control Room 或 Modern Workbench；重启后应用。", pixel_appearance_hint: "原本深色像素风、高密度终端和控制界面。", modern_appearance_hint: "对话优先，整合模型、权限、变更和证据。", save_appearance: "保存外观", appearance_saved: "外观已保存，重启后应用",
      ccswitch_title: "CC Switch 一键接入", ccswitch_hint: "读取 CC Switch 已保存的供应商和模型；只有确认后才会切换当前供应商。", ccswitch_app: "应用程序", ccswitch_provider: "CC Switch 供应商",
      discover_first: "先读取供应商", discover_models_first: "先读取模型", discover_providers: "读取供应商", discover_models: "读取模型", save_route: "创建路由", activate_provider: "切换供应商",
      provider_setup: "直接 API／中转／本地供应商", provider_setup_hint: "凭证只保存在本地安全存储；PeerBridge 数据库仅记录不可逆指纹和端点哈希。", connection_id: "连接 ID", route_class: "路由类型", endpoint: "API 端点", official: "官方", relay: "中转", local: "本地", save_provider: "保存供应商",
      model_route_setup: "读取模型并创建路由", model_route_setup_hint: "模型清单由所选供应商实时返回，不会使用预设名称冒充。", provider_saved: "供应商已保存", providers_discovered: "供应商列表已更新", models_discovered: "模型列表已更新", route_saved: "模型路由已创建", provider_activated: "CC Switch 供应商已切换",
      install_agent: "安装", update_agent: "更新", install_agent_confirm: "PeerBridge 将启动供应商的官方安装程序。是否继续？", agent_installer_started: "官方 Agent 安装程序已启动", clipboard_image_attached: "已从剪贴板添加图片", terminal_not_started: "终端尚未启动", start_terminal: "启动终端", model: "模型", reasoning_mode: "推理强度", publisher_guide: "官方安装指南", attachment_only_message: "附件", all_session_timeline: "全部终端时间轴", attachment_type_invalid: "附件格式不受支持", attachment_count_limit: "附件最多 5 个", attachment_file_size_limit: "每个附件不能超过 8 MiB", attachment_total_size_limit: "附件总大小不能超过 16 MiB", verify_audit_chain: "验证审计链", audit_chain_verified: "审计链验证通过", mark_all_read: "全部标为已读", announcements_marked_read: "公告已标为已读", usage_truncated: "所选期间的趋势数据已截断", schedule_next_run: "下次执行", expires: "到期", consumed: "已使用", ccswitch_missing_endpoint: "此供应商未设置模型 API 端点", confirm_ccswitch_switch: "确认将 {app} 切换到 {provider}？", bootstrap_failed: "本地数据加载失败；可点击刷新重试", acpx_runtime: "ACPX 运行环境", acpx_required: "Grok 和 Kimi 的持续工作阶段需要 ACPX", install_dependency: "安装依赖", dispatches: "派送", events: "事件", memories: "记忆", reason_low: "低", reason_medium: "中", reason_high: "高", reason_xhigh: "特高", reason_max: "最高", proof_required: "请先填写必要的工作 ID 或证据包路径", edit_schedule: "编辑计划", start_review_workflow: "派发互评流程", continue_history: "从此历史继续", history_continued: "已创建可写续聊房间"
    },
    en: {
      configure_agent: "Configure agent", view_terminal: "View terminal", code_files: "files", models_truncated: "Model list reached the display limit", activity_stale: "Connection is stale", appearance: "Appearance", choose_appearance: "Choose appearance", choose_appearance_hint: "Choose Pixel Control Room or Modern Workbench. The selection applies after restart.", pixel_appearance_hint: "Original dark pixel style with dense terminals and controls.", modern_appearance_hint: "Conversation-first workspace with model, permission, diff, and evidence controls.", save_appearance: "Save appearance", appearance_saved: "Appearance saved; restart to apply",
      ccswitch_title: "One-click CC Switch integration", ccswitch_hint: "Discover providers and models already saved in CC Switch. The active provider changes only after explicit confirmation.", ccswitch_app: "Application", ccswitch_provider: "CC Switch provider",
      discover_first: "Discover providers first", discover_models_first: "Discover models first", discover_providers: "Discover providers", discover_models: "Discover models", save_route: "Create route", activate_provider: "Switch provider",
      provider_setup: "Direct API, relay, or local provider", provider_setup_hint: "Credentials stay in local secure storage. The PeerBridge database stores only irreversible fingerprints and endpoint hashes.", connection_id: "Connection ID", route_class: "Route class", endpoint: "API endpoint", official: "Official", relay: "Relay", local: "Local", save_provider: "Save provider",
      model_route_setup: "Discover models and create a route", model_route_setup_hint: "The selected provider advertises the model list live; PeerBridge does not invent model names.", provider_saved: "Provider saved", providers_discovered: "Provider list updated", models_discovered: "Model list updated", route_saved: "Model route created", provider_activated: "CC Switch provider changed",
      install_agent: "Install", update_agent: "Update", install_agent_confirm: "PeerBridge will start the publisher's official installer. Continue?", agent_installer_started: "Official Agent installer started", clipboard_image_attached: "Image attached from clipboard", terminal_not_started: "Terminal not started", start_terminal: "Start terminal", model: "Model", reasoning_mode: "Reasoning effort", publisher_guide: "Official installation guide", attachment_only_message: "Attachment", all_session_timeline: "All-terminal timeline", attachment_type_invalid: "This attachment type is not supported", attachment_count_limit: "Up to 5 attachments", attachment_file_size_limit: "Each attachment must be no larger than 8 MiB", attachment_total_size_limit: "Attachments must total no more than 16 MiB", verify_audit_chain: "Verify audit chain", audit_chain_verified: "Audit chain verified", mark_all_read: "Mark all read", announcements_marked_read: "Announcements marked as read", usage_truncated: "Trend data is truncated for the selected period", schedule_next_run: "Next run", expires: "Expires", consumed: "Consumed", ccswitch_missing_endpoint: "This provider has no model API endpoint", confirm_ccswitch_switch: "Switch {app} to {provider}?", bootstrap_failed: "Local data failed to load. Use Refresh to retry.", acpx_runtime: "ACPX runtime", acpx_required: "Grok and Kimi persistent sessions require ACPX", install_dependency: "Install dependency", dispatches: "Dispatches", events: "Events", memories: "Memories", reason_low: "Low", reason_medium: "Medium", reason_high: "High", reason_xhigh: "Extra high", reason_max: "Maximum", proof_required: "Enter the required task ID or proof-bundle path first", edit_schedule: "Edit schedule", start_review_workflow: "Start review workflow", continue_history: "Continue from this history", history_continued: "Writable continuation room created"
    }
  };

  const state = {
    token: "", locale: localStorage.getItem("peerbridge.locale") || "zh-Hant", data: null, roomId: "lobby",
    view: "chat", signature: "", etag: "", timer: null, loading: false, older: [], firstRender: true, tutorialAutoChecked: false,
    attachments: [], managedAttachments: [], managedTurnAttachments: Object.create(null), feedbackAttachments: [],
    cockpitMode: "grid", selectedSessionId: "", sessionDetailTab: "terminal", usagePeriod: "30d", chatFocus: false, roomSearch: "", ccswitchProviders: [], ccswitchModels: [], providerModels: [], historyContinuationSourceRoom: "",
    worktreeDiff: null, worktreeDiffLoading: false, agentLaunchSelections: Object.create(null), preparedApprovalMode: "",
    renderSignatures: Object.create(null)
  };
  const MAX_DIFF_RENDER_LINES = 4000;
  const MAX_MODEL_OPTIONS = 500;
  const managedImageAttachmentSuffixes = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp"]);
  const managedAudioAttachmentSuffixes = new Set([".wav", ".mp3", ".m4a", ".ogg", ".flac"]);
  const managedTextAttachmentSuffixes = new Set([".csv", ".json", ".log", ".md", ".txt"]);
  const managedAttachmentSuffixes = new Set([
    ...managedImageAttachmentSuffixes,
    ...managedAudioAttachmentSuffixes,
    ...managedTextAttachmentSuffixes
  ]);
  const managedSuffixesForCapability = (multimodal) => {
    const allowed = new Set(managedTextAttachmentSuffixes);
    if (multimodal.image_input_supported !== false) {
      managedImageAttachmentSuffixes.forEach((suffix) => allowed.add(suffix));
    }
    if (multimodal.audio_input_supported === true) {
      managedAudioAttachmentSuffixes.forEach((suffix) => allowed.add(suffix));
    }
    return allowed;
  };
  const workbenchSessionStorageKey = "peerbridge.workbench.accessToken";
  const byId = (id) => document.getElementById(id);
  const t = (key) => (translations[state.locale] || translations.en)[key]
    || (integrationTranslations[state.locale] || integrationTranslations.en)[key]
    || (capabilityTranslations[state.locale] || capabilityTranslations.en)[key]
    || (controlTranslations[state.locale] || controlTranslations.en)[key]
    || key;
  const localizedErrorMessage = (value) => {
    const message = String(value || "").trim();
    const language = state.locale in translations ? state.locale : "en";
    const exact = {
      "MCP message path unavailable": {
        "zh-Hant": "MCP 訊息通道目前無法使用",
        "zh-Hans": "MCP 消息通道当前不可用",
        en: "MCP message path unavailable"
      },
      "CC Switch provider has no saved API key": {
        "zh-Hant": "CC Switch 供應商尚未保存 API Key",
        "zh-Hans": "CC Switch 供应商尚未保存 API Key",
        en: "CC Switch provider has no saved API key"
      },
      "CC Switch provider has no model-discovery endpoint": {
        "zh-Hant": "CC Switch 供應商未設定可讀取模型的 API 端點",
        "zh-Hans": "CC Switch 供应商未设置可读取模型的 API 端点",
        en: "CC Switch provider has no model-discovery endpoint"
      },
      "CC Switch provider credential was rejected": {
        "zh-Hant": "CC Switch 供應商憑證被拒絕",
        "zh-Hans": "CC Switch 供应商凭证被拒绝",
        en: "CC Switch provider credential was rejected"
      },
      "CC Switch provider quota or rate limit was reached": {
        "zh-Hant": "CC Switch 供應商已達配額或速率限制",
        "zh-Hans": "CC Switch 供应商已达到配额或速率限制",
        en: "CC Switch provider quota or rate limit was reached"
      }
    };
    if (exact[message]) return exact[message][language];
    const status = Number(message);
    const byStatus = {
      400: { "zh-Hant": "要求內容無效", "zh-Hans": "请求内容无效", en: "Invalid request" },
      401: { "zh-Hant": "未通過本機驗證", "zh-Hans": "未通过本地验证", en: "Local authorization failed" },
      403: { "zh-Hant": "沒有執行此操作的權限", "zh-Hans": "没有执行此操作的权限", en: "This action is not authorized" },
      404: { "zh-Hant": "找不到指定資源", "zh-Hans": "找不到指定资源", en: "Requested resource not found" },
      409: { "zh-Hant": "操作與目前狀態衝突", "zh-Hans": "操作与当前状态冲突", en: "Action conflicts with the current state" },
      422: { "zh-Hant": "要求內容未通過驗證", "zh-Hans": "请求内容未通过验证", en: "Request validation failed" },
      429: { "zh-Hant": "操作過於頻繁，請稍後再試", "zh-Hans": "操作过于频繁，请稍后重试", en: "Too many requests; try again shortly" },
      500: { "zh-Hant": "本機服務執行失敗", "zh-Hans": "本地服务执行失败", en: "Local service failed" },
      503: { "zh-Hant": "本機服務目前無法使用", "zh-Hans": "本地服务当前不可用", en: "Local service is unavailable" }
    };
    return byStatus[status]?.[language] || message || byStatus[500][language];
  };
  const node = (tag, className, text) => { const el = document.createElement(tag); if (className) el.className = className; if (text !== undefined) el.textContent = text; return el; };
  const compact = (value) => new Intl.NumberFormat(state.locale, { notation: "compact", maximumFractionDigits: 1 }).format(Number(value || 0));
  const timeLabel = (value) => { const d = new Date(value); return Number.isNaN(d.getTime()) ? "" : new Intl.DateTimeFormat(state.locale, { hour: "2-digit", minute: "2-digit" }).format(d); };
  const epochLabel = (value) => { const d = new Date(Number(value || 0) * 1000); return Number.isNaN(d.getTime()) || Number(value || 0) <= 0 ? "" : new Intl.DateTimeFormat(state.locale, { dateStyle: "short", timeStyle: "short" }).format(d); };
  const initials = (value) => String(value || "?").split(/[-_.]/).filter(Boolean).slice(0, 2).map((part) => part[0]).join("").toUpperCase();
  const dispatchLabel = (status) => t(`dispatch_${String(status || "pending").replaceAll("-", "_")}`);
  const dispatchTone = (status) => {
    const value = String(status || "pending").toLowerCase();
    if (value === "completed") return "success";
    if (value === "failed" || value === "dead_letter") return "danger";
    if (value.includes("retry")) return "warning";
    if (value === "claimed" || value === "running") return "active";
    return "muted";
  };

  const makeRequestId = () => {
    const raw = crypto.randomUUID
      ? crypto.randomUUID().replaceAll("-", "")
      : `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}${Math.random().toString(36).slice(2)}`;
    return raw.padEnd(16, "0");
  };

  const displayValue = (value) => {
    if (value === true) return t("enabled");
    if (value === false) return t("disabled");
    if (value === null || value === undefined || value === "") return "--";
    return String(value);
  };
  const workflowLabel = (entry) => {
    const workflowId = String(entry?.workflow_id || "");
    const key = `workflow_${workflowId.replaceAll("-", "_")}`;
    const localized = t(key);
    return localized === key ? String(entry?.label || workflowId) : localized;
  };

  const managedRoleValues = () => {
    const values = state.data?.managed_agent_roles || ["equal-participant", "researcher", "implementer", "reviewer", "investigator", "planner", "auditor"];
    return values.filter((value) => value && value !== "custom");
  };
  const roomRoleValues = () => ["equal-participant", "researcher", "implementer", "reviewer"];
  const roleLabel = (value) => t(`role_${String(value || "equal-participant").replaceAll("-", "_")}`);
  const terminalStatus = (value) => new Set(["complete", "completed", "done", "verified", "closed", "passed", "stopped", "failed", "cancelled", "canceled", "unavailable"]).has(String(value || "").toLowerCase());

  const eventActivity = (event) => {
    const text = `${event?.kind || ""} ${event?.stream || ""} ${event?.summary || ""} ${event?.state_after || ""}`.toLowerCase();
    if (/fail|error|rejected|denied/.test(text)) return { key: "activity_failed", tone: "danger" };
    if (/web|browser|search|crawl|http|url/.test(text)) return { key: "activity_searching", tone: "working" };
    if (/reason|think|plan|analy/.test(text)) return { key: "activity_reasoning", tone: "working" };
    if (/edit|write|patch|apply|save|file changed/.test(text)) return { key: "activity_editing", tone: "working" };
    if (/read|open|inspect|view|find|grep|\brg\b|\bcat\b/.test(text)) return { key: "activity_reading", tone: "working" };
    if (/wait|pending|queued|retry|blocked/.test(text)) return { key: "activity_waiting", tone: "waiting" };
    if (/complete|completed|final|done|verified|passed/.test(text)) return { key: "activity_completed", tone: "online" };
    return { key: "activity_running", tone: "working" };
  };

  function latestByTime(rows, fields = ["updated_utc", "last_seen_utc", "created_utc", "started_utc"]) {
    return [...rows].sort((left, right) => {
      const leftValue = fields.map((field) => Date.parse(left?.[field] || "")).find(Number.isFinite) || 0;
      const rightValue = fields.map((field) => Date.parse(right?.[field] || "")).find(Number.isFinite) || 0;
      return rightValue - leftValue;
    })[0] || null;
  }

  function recentObservable(row, fields = ["updated_utc", "last_seen_utc", "created_utc", "started_utc"], maxAgeMs = 180000) {
    if (!row) return false;
    const timestamp = fields.map((field) => Date.parse(row?.[field] || "")).find(Number.isFinite);
    return Number.isFinite(timestamp) && Date.now() - timestamp >= 0 && Date.now() - timestamp <= maxAgeMs;
  }

  function agentRuntime(agentId) {
    const data = state.data || {};
    const member = (data.members || []).find((row) => row.agent_id === agentId) || null;
    const presence = (data.cockpit?.presence || []).find((row) => row.agent_id === agentId) || null;
    const sessions = (data.cockpit?.sessions || []).filter((row) => (row.owner_agent_id || row.adapter_id) === agentId);
    const session = latestByTime(sessions);
    const sessionEvents = (data.cockpit?.events || []).filter((row) => row.session_id && row.session_id === session?.session_id);
    const event = latestByTime(sessionEvents, ["created_utc"]);
    const dispatch = latestByTime((data.dispatches || []).filter((row) => row.agent_id === agentId));
    const work = latestByTime((data.work_updates || []).filter((row) => row.agent_id === agentId));
    const sessionState = String(session?.state || session?.status || session?.lifecycle_state || "").toLowerCase();
    const dispatchState = String(dispatch?.status || "").toLowerCase();
    const workState = String(work?.status || "").toLowerCase();
    let activity = { key: "activity_offline", tone: "offline" };
    let summary = "";
    const sessionFresh = Boolean(session?.managed) || recentObservable(
      session,
      ["last_seen_utc", "updated_utc", "started_utc"],
      180000
    );
    const eventFresh = recentObservable(event, ["created_utc"], 180000);
    if (session && !terminalStatus(sessionState) && sessionFresh) {
      activity = event && eventFresh ? eventActivity(event) : { key: "activity_running", tone: "working" };
      summary = event && eventFresh ? event.summary || "" : session.terminal_detail || session.terminal_outcome || "";
    } else if (session && !terminalStatus(sessionState)) {
      activity = { key: "activity_stale", tone: "offline" };
      summary = t("activity_stale");
    } else if ((recentObservable(dispatch) && ["claimed", "running", "active", "processing"].includes(dispatchState)) || (recentObservable(work) && ["claimed", "running", "active", "review"].includes(workState))) {
      activity = { key: "activity_running", tone: "working" };
      summary = work?.summary || dispatch?.error_code || "";
    } else if (recentObservable(dispatch) && (dispatchState.includes("retry") || ["pending", "queued"].includes(dispatchState))) {
      activity = { key: "activity_waiting", tone: "waiting" };
    } else if ((recentObservable(dispatch, ["completed_utc", "updated_utc"]) && ["failed", "dead_letter"].includes(dispatchState)) || sessionState === "failed") {
      activity = { key: "activity_failed", tone: "danger" };
      summary = dispatch?.error_code || session?.terminal_outcome || "";
    } else if (presence || member?.online) {
      activity = { key: "activity_idle", tone: "online" };
      if (event) summary = event.summary || "";
    } else if (session && terminalStatus(sessionState)) {
      activity = sessionState === "failed" ? { key: "activity_failed", tone: "danger" } : { key: "activity_completed", tone: "online" };
      summary = session.terminal_outcome || event?.summary || "";
    }
    const route = (data.routes || []).find((row) => row.agent_id === agentId && row.enabled !== false) || null;
    return {
      agentId,
      member,
      presence,
      session,
      event,
      dispatch,
      activity,
      summary,
      model: session?.model_id || presence?.model_id || member?.model_id || route?.model_id || "",
      reasoning: presence?.reasoning_mode || member?.reasoning_mode || route?.reasoning_mode || "",
      permission: session?.permission_tier || session?.session_authorization?.permission_tier || "",
      route: session?.requested_route || member?.route_profile_id || route?.route_id || ""
    };
  }

  const managedAgentMatches = (managedAgentId, routeAgentId) => {
    const left = String(managedAgentId || "").toLowerCase();
    const right = String(routeAgentId || "").toLowerCase();
    if (!left || !right) return false;
    if (left === right) return true;
    if (left === "claude-code") return right.includes("claude");
    if (left === "kimi-code") return right.includes("kimi");
    return right.includes(left);
  };

  const managedAgentForRoomAgent = (routeAgentId) => (
    (state.data?.managed_agent_catalog || []).find(
      (entry) => entry.primary && managedAgentMatches(entry.agent_id, routeAgentId)
    ) || null
  );

  function updateComposerPermissionControls() {
    const select = byId("composer-permission");
    if (!select) return;
    const recipient = byId("recipient")?.value || "*";
    const managed = recipient === "*" ? null : managedAgentForRoomAgent(recipient);
    const launchable = new Set(
      (managed?.permission_tiers || [])
        .filter((tier) => tier.launchable)
        .map((tier) => tier.tier_id)
    );
    Array.from(select.options).forEach((option) => {
      const requiredTier = option.value === "full-access" ? "full-development" : "edit";
      option.disabled = option.value !== "approval-required"
        && (!managed?.installed || !launchable.has(requiredTier));
    });
    if (select.selectedOptions[0]?.disabled) select.value = "approval-required";
    select.title = recipient === "*" || !managed?.installed
      ? t("managed_permission_requires_agent")
      : t("permission_mode");
  }

  function managedRouteOptions(agentId) {
    const routes = (state.data?.routes || []).filter((route) => route.enabled !== false && managedAgentMatches(agentId, route.agent_id));
    const entry = (state.data?.managed_agent_catalog || []).find((row) => row.agent_id === agentId);
    const rows = routes.map((route) => [
      route.route_id,
      [route.model_id, route.reasoning_mode, route.provider_id].filter(Boolean).join(" · ") || route.route_id
    ]);
    const observedModel = entry?.receipt?.observed_model || "";
    if (observedModel && !rows.some(([value]) => value === observedModel)) rows.unshift([observedModel, observedModel]);
    return [["", t("no_route")], ...rows];
  }

  function replaceSelectOptions(select, rows, selectedValue = "") {
    if (!select) return;
    const prior = selectedValue || select.value;
    select.replaceChildren();
    rows.forEach(([value, label]) => {
      const option = node("option", "", label); option.value = value; select.append(option);
    });
    select.value = Array.from(select.options).some((option) => option.value === prior) ? prior : (select.options[0]?.value || "");
  }

  function authorizationValue() {
    return ["Bearer", state.token].join(" ");
  }

  const localRequestTimeoutMessage = () => state.locale === "zh-Hans"
    ? "本地请求超时；请在活动记录中核对最终结果。"
    : state.locale === "en"
      ? "The local request timed out. Check Activity for the terminal result."
      : "本機請求逾時；請在活動記錄中核對最終結果。";

  async function fetchWithTimeout(resource, options = {}, timeoutMs = 30000) {
    const controller = new AbortController();
    const timeoutId = window.setTimeout(
      () => controller.abort(),
      Math.max(1000, Number(timeoutMs) || 30000)
    );
    try {
      return await fetch(resource, { ...options, signal: controller.signal });
    } catch (error) {
      if (error?.name === "AbortError") throw new Error(localRequestTimeoutMessage());
      throw error;
    } finally {
      window.clearTimeout(timeoutId);
    }
  }

  async function postAction(path, payload, { timeoutMs = 30000 } = {}) {
    const response = await fetchWithTimeout(path, {
      method: "POST",
      headers: { Authorization: authorizationValue(), "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: makeRequestId(), ...payload })
    }, timeoutMs);
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(localizedErrorMessage(result.error || String(response.status)));
    state.etag = "";
    await fetchState(true);
    return result;
  }

  function renderMetricStrip(containerId, rows) {
    const container = byId(containerId); if (!container) return; container.replaceChildren();
    rows.forEach(([label, value, detail]) => {
      const item = node("div", "metric-item");
      item.append(node("span", "", label), node("strong", "", displayValue(value)));
      if (detail) item.append(node("small", "", detail));
      container.append(item);
    });
  }

  function renderRecordList(containerId, rows, mapper) {
    const container = byId(containerId); if (!container) return; container.replaceChildren();
    if (!rows.length) { container.append(node("div", "panel-empty", t("no_records"))); return; }
    rows.forEach((entry) => {
      const data = mapper(entry); const row = node("article", "record-row");
      const head = node("div", "record-head");
      head.append(node("strong", "", data.title || "--"));
      if (data.status) head.append(node("span", `status-badge ${dispatchTone(data.status)}`, displayValue(data.status)));
      row.append(head);
      if (data.body) row.append(node("p", "", data.body));
      const metaValues = (data.meta || []).filter((value) => value !== null && value !== undefined && value !== "");
      if (metaValues.length) { const meta = node("div", "record-meta"); metaValues.forEach((value) => meta.append(node("span", "", displayValue(value)))); row.append(meta); }
      container.append(row);
    });
  }

  function applyLocale() {
    document.documentElement.lang = state.locale;
    if (byId("locale-select")) byId("locale-select").value = state.locale;
    document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
    document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => { el.placeholder = t(el.dataset.i18nPlaceholder); });
    document.querySelectorAll("[data-i18n-title]").forEach((el) => {
      const label = t(el.dataset.i18nTitle);
      el.title = label;
      el.setAttribute("aria-label", label);
    });
    render();
    setChatFocus(state.chatFocus);
  }

  function toast(message) {
    const el = byId("toast"); el.textContent = message; el.hidden = false;
    window.clearTimeout(toast.timer); toast.timer = window.setTimeout(() => { el.hidden = true; }, 3200);
  }

  function openTutorial() {
    const dialog = byId("tutorial-dialog");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  async function closeTutorial(completed) {
    const dialog = byId("tutorial-dialog");
    if (completed) {
      try {
        await postAction("/api/preferences/save", {
          locale: state.locale,
          tutorial_completed: true
        });
      } catch (error) {
        toast(`${t("action_failed")}: ${error.message}`);
        return;
      }
    }
    dialog.close();
  }

  function setConnection(online) {
    const el = byId("connection-state"); el.classList.toggle("online", online); el.classList.toggle("offline", !online);
    el.querySelector("span").textContent = online ? t("online") : t("offline");
  }

  function setChatFocus(enabled) {
    state.chatFocus = Boolean(enabled);
    byId("app").classList.toggle("chat-focus", state.chatFocus);
    const button = byId("chat-focus-button");
    const label = t(state.chatFocus ? "exit_fullscreen" : "fullscreen");
    button.textContent = state.chatFocus ? "↙" : "⛶";
    button.title = label;
    button.setAttribute("aria-label", label);
    if (state.chatFocus && state.view !== "chat") {
      state.view = "chat";
      render();
    }
  }

  function automationText(mode) {
    return t({ off: "automation_off", once: "automation_once", discussion: "automation_discussion" }[mode] || "automation_off");
  }

  function renderRooms() {
    const rooms = state.data?.rooms || [];
    const query = state.roomSearch.trim().toLocaleLowerCase(state.locale);
    const visibleRooms = query
      ? rooms.filter((room) => `${room.name || ""} ${room.room_id || ""}`.toLocaleLowerCase(state.locale).includes(query))
      : rooms;
    const signature = JSON.stringify([state.locale, state.data?.room_id, query, rooms]);
    if (state.renderSignatures.rooms === signature) return;
    state.renderSignatures.rooms = signature;
    const list = byId("room-list"); list.replaceChildren();
    visibleRooms.forEach((room) => {
      const imported = room.room_kind === "imported-history";
      const button = node("button", "room-item" + (imported ? " history-room" : "") + (room.room_id === state.data.room_id ? " active" : "")); button.type = "button";
      const detail = imported
        ? `${t("imported_room")} · ${room.provider || "--"} · ${room.message_count} ${t("messages")}`
        : `${room.active_member_count} ${t("agents")} · ${room.message_count} ${t("messages")} · ${timeLabel(room.updated_utc) || "--"}`;
      const copy = node("span", "room-copy"); copy.append(node("strong", "", room.name || room.room_id)); copy.append(node("span", "", detail));
      button.append(copy, node("span", "room-badge", String(room.message_count || 0)));
      button.addEventListener("click", () => { state.view = "chat"; state.roomId = room.room_id; state.older = []; state.signature = ""; state.firstRender = true; fetchState(true); closeMobilePanels(); });
      list.append(button);
    });
    if (!visibleRooms.length) list.append(node("div", "panel-empty", t("no_room_matches")));
    byId("room-search-status").textContent = query ? `${visibleRooms.length} / ${rooms.length}` : "";
  }

  function messageDispatches(messageId) {
    return (state.data?.dispatches || []).filter((row) => row.message_id === messageId);
  }

  function groupMessages(rows) {
    const groups = new Map();
    rows.forEach((message) => {
      const key = message.task_id || `message:${message.message_id}`;
      if (!groups.has(key)) groups.set(key, {
        taskId: key,
        messages: [],
        sourceMessages: [],
        visibleRootKeys: new Set(),
        firstSequence: Number(message.sequence || 0)
      });
      const group = groups.get(key);
      group.sourceMessages.push(message);
      group.firstSequence = Math.min(group.firstSequence, Number(message.sequence || 0));
      if (!message.reply_to) {
        const rootKey = JSON.stringify([
          message.sender,
          message.task_id,
          message.subject,
          message.body,
          message.created_utc,
          message.discussion_id,
          message.discussion_round,
          message.discussion_role
        ]);
        if (group.visibleRootKeys.has(rootKey)) return;
        group.visibleRootKeys.add(rootKey);
      }
      group.messages.push(message);
    });
    return [...groups.values()].sort((a, b) => a.firstSequence - b.firstSequence);
  }

  function renderMessageCard(message) {
    const wrap = node("article", "message" + (message.sender === "human-operator" ? " own" : " agent-reply"));
    wrap.append(node("div", "message-avatar", initials(message.sender)));
    const main = node("div", "message-main"); const meta = node("div", "message-meta");
    const copy = node("button", "message-copy", "⧉"); copy.type = "button"; copy.title = t("copy"); copy.setAttribute("aria-label", t("copy"));
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(String(message.body || ""));
      } catch (_error) {
        const fallback = node("textarea"); fallback.value = String(message.body || ""); fallback.setAttribute("readonly", ""); fallback.style.position = "fixed"; fallback.style.opacity = "0"; document.body.append(fallback); fallback.select(); document.execCommand("copy"); fallback.remove();
      }
      toast(t("copied"));
    });
    meta.append(node("strong", "", message.sender || "Agent"), node("span", "", timeLabel(message.created_utc)), copy);
    main.append(meta);
    if (message.subject) main.append(node("div", "message-subject", message.subject));
    main.append(node("p", "message-body", message.body));
    const route = node("div", "message-route-row");
    const routeValues = [message.observed_provider_id || message.requested_provider_id, message.observed_model_id || message.requested_model_id, message.observed_reasoning_mode || message.requested_reasoning_mode];
    routeValues.filter(Boolean).forEach((value) => route.append(node("span", "route-chip", value)));
    if (message.artifact_count) route.append(node("span", "route-chip", `${message.artifact_count} ${t("files")}`));
    if (message.content_sha256) route.append(node("span", "route-hash", message.content_sha256.slice(0, 10)));
    if (route.childElementCount) main.append(route);
    wrap.append(main);
    return wrap;
  }

  function renderDispatchRail(messages) {
    const dispatches = messages.flatMap((message) => messageDispatches(message.message_id));
    if (!dispatches.length) return null;
    const rail = node("div", "dispatch-rail");
    dispatches.forEach((dispatch) => {
      const item = node("div", `dispatch-chip ${dispatchTone(dispatch.status)}`);
      item.append(node("i", ""), node("strong", "", dispatch.agent_id || "Agent"), node("span", "", dispatchLabel(dispatch.status)));
      if (dispatch.attempt_count > 1) item.append(node("small", "", `${t("attempt")} ${dispatch.attempt_count}`));
      rail.append(item);
    });
    return rail;
  }

  function renderMessages() {
    const list = byId("message-list"); const timeline = byId("timeline");
    const nearBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 110;
    const rows = [...state.older, ...(state.data?.messages || [])];
    const signature = JSON.stringify([
      state.locale,
      state.data?.room_id,
      rows,
      state.data?.dispatches || [],
      Boolean(state.data?.page?.has_older)
    ]);
    if (state.renderSignatures.messages === signature) return;
    state.renderSignatures.messages = signature;
    list.replaceChildren();
    groupMessages(rows).forEach((group) => {
      const section = node("section", "collaboration-run");
      const header = node("header", "run-header");
      const title = node("div", "run-title"); title.append(node("span", "run-kicker", t("current_round")), node("strong", "", group.taskId));
      const participants = new Set(group.messages.map((message) => message.sender).filter(Boolean));
      const replyCount = group.messages.filter((message) => message.reply_to).length;
      const stats = node("div", "run-stats"); stats.append(node("span", "", `${replyCount} ${t("replies")}`), node("span", "", `${participants.size} ${t("participants")}`));
      header.append(title, stats); section.append(header);
      const humanMessages = group.messages.filter((message) => message.sender === "human-operator");
      const agentMessages = group.messages.filter((message) => message.sender !== "human-operator");
      const promptList = node("div", "prompt-list"); humanMessages.forEach((message) => promptList.append(renderMessageCard(message))); if (humanMessages.length) section.append(promptList);
      const dispatchRail = renderDispatchRail(group.sourceMessages); if (dispatchRail) section.append(dispatchRail);
      if (agentMessages.length) { const responseGrid = node("div", "response-grid"); agentMessages.forEach((message) => responseGrid.append(renderMessageCard(message))); section.append(responseGrid); }
      list.append(section);
    });
    const empty = rows.length === 0; byId("empty-chat").hidden = !empty; byId("load-older").hidden = !(state.data?.page?.has_older);
    if (state.firstRender || nearBottom) requestAnimationFrame(() => { timeline.scrollTop = timeline.scrollHeight; state.firstRender = false; });
  }

  function renderAgents() {
    const members = state.data?.members || [];
    const readOnlyRoom = Boolean(state.data?.history_import?.selected);
    const signature = JSON.stringify([
      state.locale,
      state.data?.room_id,
      members,
      state.data?.routes || [],
      state.data?.cockpit?.presence || []
    ]);
    if (state.renderSignatures.agents === signature) return;
    state.renderSignatures.agents = signature;
    const list = byId("agent-list"); list.replaceChildren();
    members.forEach((member) => {
      const runtime = agentRuntime(member.agent_id);
      const row = node("div", "agent-row"); row.append(node("div", "agent-avatar", initials(member.agent_id)));
      const copy = node("div", "agent-copy");
      copy.append(node("strong", "", member.agent_id));
      const modelLine = [runtime.model || t("unknown"), runtime.reasoning].filter(Boolean).join(" · ");
      copy.append(node("span", "", modelLine));
      const runtimeMeta = node("div", "agent-runtime-meta");
      runtimeMeta.append(node("span", "", `${t("permission")}: ${runtime.permission ? permissionTierLabel(runtime.permission) : "--"}`));
      runtimeMeta.append(node("span", "", `${t("role")}: ${member.role_label || roleLabel(member.role_id)}`));
      copy.append(runtimeMeta);
      row.append(copy);
      if (member.agent_id !== "human-operator") {
        const role = node("select", "role-select"); role.setAttribute("aria-label", `${t("role")}: ${member.agent_id}`);
        roomRoleValues().forEach((value) => { const option = node("option", "", roleLabel(value)); option.value = value; role.append(option); });
        role.value = Array.from(role.options).some((option) => option.value === member.role_id) ? member.role_id : "equal-participant";
        role.disabled = readOnlyRoom;
        role.addEventListener("change", async () => {
          role.disabled = true;
          try {
            await postAction("/api/room/member-role", { room_id: state.data.room_id, agent_id: member.agent_id, role_id: role.value, role_label: "" });
            toast(t("role_saved"));
          } catch (error) { toast(`${t("action_failed")}: ${error.message}`); }
          finally { role.disabled = false; }
        });
        row.append(role);
      }
      const status = node("span", `agent-state ${runtime.activity.tone}`);
      status.append(node("i", "presence-dot"), node("span", "", t(runtime.activity.key)));
      status.title = runtime.summary || t(runtime.activity.key);
      row.append(status); list.append(row);
    });
    byId("seat-count").textContent = `${members.filter((m) => m.online).length} / ${members.length}`;
    const select = byId("recipient"); const selected = select.value || "*"; select.replaceChildren();
    const all = node("option", "", t("all_agents")); all.value = "*"; select.append(all);
    members.filter((m) => m.agent_id !== "human-operator").forEach((member) => { const option = node("option", "", member.agent_id); option.value = member.agent_id; select.append(option); });
    select.value = Array.from(select.options).some((o) => o.value === selected) ? selected : "*";
    updateComposerPermissionControls();
    renderSeatControls();
  }

  function renderSeatControls() {
    const members = state.data?.members || [];
    const routes = (state.data?.routes || []).filter((route) => route.enabled !== false && route.agent_id);
    const presence = state.data?.cockpit?.presence || [];
    const agentIds = [...new Set([
      ...routes.map((route) => route.agent_id),
      ...members.map((member) => member.agent_id),
      ...presence.map((entry) => entry.agent_id)
    ].filter((value) => value && value !== "human-operator"))].sort();
    const agentSelect = byId("seat-agent"); const priorAgent = agentSelect.value;
    replaceSelectOptions(agentSelect, agentIds.map((value) => [value, value]), priorAgent);
    const renderRouteOptions = () => {
      const agentId = agentSelect.value;
      const rows = routes.filter((route) => route.agent_id === agentId).map((route) => [
        route.route_id,
        [route.provider_id, route.model_id, route.reasoning_mode].filter(Boolean).join(" · ") || route.route_id
      ]);
      replaceSelectOptions(byId("seat-route"), [["", t("no_route")], ...rows]);
    };
    renderRouteOptions();
    agentSelect.onchange = renderRouteOptions;
    replaceSelectOptions(byId("seat-role"), roomRoleValues().map((value) => [value, roleLabel(value)]), byId("seat-role").value || "equal-participant");
    const removable = members.filter((member) => member.agent_id !== "human-operator");
    replaceSelectOptions(byId("seat-remove-member"), removable.length ? removable.map((member) => [member.agent_id, member.agent_id]) : [["", t("no_removable_member")]]);
    const readOnlyRoom = Boolean(state.data?.history_import?.selected);
    ["seat-agent", "seat-route", "seat-role", "seat-remove-member"].forEach((id) => { byId(id).disabled = readOnlyRoom; });
    byId("seat-add").disabled = readOnlyRoom || !agentIds.length;
    byId("seat-remove").disabled = readOnlyRoom || !removable.length;
  }

  function renderTasks() {
    const rows = state.data?.tasks || []; byId("task-count").textContent = String(rows.length); byId("tasks-summary").textContent = `${rows.length} ${t("tasks")}`;
    if (byId("board-task-count")) byId("board-task-count").textContent = String(rows.length);
    const list = byId("tasks-list"); list.replaceChildren();
    if (!rows.length) { list.append(node("div", "empty-state", t("no_tasks"))); return; }
    rows.forEach((task) => { const row = node("div", "summary-row"); const title = node("div", ""); title.append(node("strong", "", task.task_id), node("div", "message-subject", task.summary)); row.append(title, node("span", "", `${t("status")}: ${task.status || "--"}`), node("span", "", `${t("claimed_by")}: ${task.claimed_by || "--"}`)); list.append(row); });
  }

  function renderSessionDetails(sessions, events) {
    const workspace = byId("session-workspace");
    workspace.className = `session-workspace mode-${state.cockpitMode}`;
    byId("session-detail-tabs").hidden = state.cockpitMode === "timeline";
    document.querySelectorAll("[data-cockpit-mode]").forEach((button) => button.classList.toggle("active", button.dataset.cockpitMode === state.cockpitMode));
    document.querySelectorAll("[data-session-tab]").forEach((button) => button.classList.toggle("active", button.dataset.sessionTab === state.sessionDetailTab));
    const selected = sessions.find((entry) => entry.session_id === state.selectedSessionId) || null;
    const detail = byId("session-detail-content"); const eventList = byId("session-event-list");
    detail.replaceChildren(); eventList.replaceChildren();
    if (state.cockpitMode === "timeline") {
      byId("session-detail-title").textContent = t("all_session_timeline");
      byId("session-detail-subtitle").textContent = `${sessions.length} ${t("managed_sessions")}`;
      byId("session-event-count").textContent = String(events.length);
      detail.hidden = true; eventList.hidden = false;
      const sessionNames = new Map(sessions.map((entry) => [entry.session_id, entry.display_name || entry.client_name || entry.owner_agent_id || entry.session_id]));
      const ordered = [...events].sort((left, right) => {
        const timeDifference = Date.parse(right.created_utc || "") - Date.parse(left.created_utc || "");
        return Number.isFinite(timeDifference) && timeDifference !== 0
          ? timeDifference
          : Number(right.sequence || 0) - Number(left.sequence || 0);
      });
      renderRecordList("session-event-list", ordered, (entry) => ({
        title: `${sessionNames.get(entry.session_id) || entry.session_id || "Agent"} · ${entry.summary || entry.kind || entry.stream || "--"}`,
        status: entry.kind,
        body: [entry.stream, entry.state_after, entry.source_type].filter(Boolean).join(" · "),
        meta: [entry.sequence, timeLabel(entry.created_utc), (entry.sha256 || "").slice(0, 12)]
      }));
      return;
    }
    if (!selected) {
      byId("session-detail-title").textContent = t("session_details");
      byId("session-detail-subtitle").textContent = t("no_session_selected");
      byId("session-event-count").textContent = "0";
      eventList.hidden = true;
      detail.append(node("div", "panel-empty", t("no_session_selected")));
      return;
    }
    const selectedEvents = events.filter((entry) => entry.session_id === selected.session_id);
    byId("session-detail-title").textContent = selected.display_name || selected.client_name || selected.session_id;
    byId("session-detail-subtitle").textContent = [selected.owner_agent_id, selected.model_id || selected.requested_route, displayValue(selected.state || selected.status)].filter(Boolean).join(" · ");
    byId("session-event-count").textContent = String(selectedEvents.length);
    if (state.sessionDetailTab === "activity") {
      detail.hidden = true; eventList.hidden = false;
      renderRecordList("session-event-list", selectedEvents, (entry) => ({
        title: entry.summary || entry.kind || entry.stream,
        status: entry.kind,
        body: [entry.stream, entry.state_after, entry.source_type].filter(Boolean).join(" · "),
        meta: [entry.sequence, timeLabel(entry.created_utc), (entry.sha256 || "").slice(0, 12)]
      }));
      return;
    }
    detail.hidden = false; eventList.hidden = true;
    if (state.sessionDetailTab === "terminal") {
      const terminal = node("pre", "session-terminal");
      const eventText = selectedEvents.slice(0, 40).reverse().map((entry) => {
        const stamp = timeLabel(entry.created_utc) || "--:--";
        const channel = entry.stream || entry.kind || "event";
        return `[${stamp}] ${channel}\n${entry.summary || entry.state_after || "--"}`;
      }).join("\n\n");
      terminal.textContent = [selected.terminal_detail || selected.terminal_outcome || "", eventText].filter(Boolean).join("\n\n") || t("no_records");
      detail.append(terminal);
      return;
    }
    if (state.sessionDetailTab === "final") {
      const finalEvent = selectedEvents.find((entry) => /final|response|reply|complete/i.test(`${entry.kind} ${entry.stream}`) && entry.summary);
      const finalText = selected.terminal_detail || selected.terminal_outcome || finalEvent?.summary || "";
      const finalPanel = node("article", "session-final-answer");
      finalPanel.append(node("h4", "", t("final_answer")), node("p", "", finalText || t("no_final_answer")));
      if (finalEvent?.sha256) finalPanel.append(node("small", "", `${t("sha_binding")}: ${finalEvent.sha256.slice(0, 16)}`));
      detail.append(finalPanel);
      return;
    }
    const evidence = node("dl", "session-evidence-grid");
    const receiptRows = [
      [t("agent"), selected.owner_agent_id],
      [t("transport"), selected.adapter_id || selected.source_type],
      [t("observed_model"), selected.model_id || selected.requested_route],
      [t("role"), selected.role_label || selected.role_id],
      [t("session_contract"), selected.session_contract?.mode || selected.input_mode || selected.execution_mode],
      [t("input_transport"), selected.session_contract?.input_transport],
      [t("attachment_delivery"), (selected.attachment_delivery_receipts || []).length],
      [t("vision_verification"), (selected.vision_verification_receipts || []).at(-1)?.status],
      [t("token_usage"), selected.usage?.total_tokens],
      [t("sha_binding"), selected.sha256]
    ];
    receiptRows.forEach(([label, value]) => { evidence.append(node("dt", "", label), node("dd", "", displayValue(value))); });
    detail.append(evidence);
  }

  function renderAgentRuntimeStrip() {
    const container = byId("agent-runtime-strip");
    if (!container) return;
    container.replaceChildren();
    const catalog = (state.data?.managed_agent_catalog || []).filter((entry) => entry.primary);
    const ids = [...new Set([
      ...catalog.map((entry) => entry.agent_id),
      ...(state.data?.members || []).map((entry) => entry.agent_id).filter((value) => value !== "human-operator"),
      ...(state.data?.cockpit?.presence || []).map((entry) => entry.agent_id)
    ].filter(Boolean))];
    const runtimes = ids.map(agentRuntime);
    const working = runtimes.filter((runtime) => runtime.activity.tone === "working").length;
    const online = runtimes.filter((runtime) => new Set(["working", "online", "waiting"]).has(runtime.activity.tone)).length;
    byId("agent-runtime-summary").textContent = `${working} ${t("activity_running")} · ${online} / ${runtimes.length}`;
    if (!runtimes.length) {
      container.append(node("div", "panel-empty", t("no_records")));
      return;
    }
    runtimes.forEach((runtime) => {
      const entry = catalog.find((candidate) => candidate.agent_id === runtime.agentId) || null;
      const card = node("article", `agent-runtime-card ${runtime.activity.tone}`);
      const heading = node("div", "agent-runtime-heading");
      const identity = node("div", "agent-runtime-identity");
      identity.append(node("span", "agent-avatar", initials(entry?.label || runtime.agentId)));
      const copy = node("div", ""); copy.append(node("strong", "", entry?.label || runtime.agentId), node("small", "", runtime.agentId)); identity.append(copy);
      const status = node("span", `agent-live-state ${runtime.activity.tone}`);
      status.append(node("i", "presence-dot"), node("span", "", t(runtime.activity.key)));
      heading.append(identity, status); card.append(heading);

      const activity = node("p", "agent-observable-activity", runtime.summary || t(runtime.activity.key));
      activity.title = runtime.summary || t(runtime.activity.key);
      card.append(activity);

      if (!entry) {
        const facts = node("div", "agent-runtime-facts");
        facts.append(
          node("span", "", t("model")), node("span", "", runtime.model || "--"),
          node("span", "", t("permission")), node("span", "", runtime.permission ? permissionTierLabel(runtime.permission) : "--")
        );
        card.append(facts); container.append(card);
        return;
      }

      const controls = node("div", "agent-runtime-controls");
      const routeLabel = node("label", "compact-field"); routeLabel.append(node("span", "", t("model_route")));
      const routeSelect = node("select", "agent-route-select");
      const saved = state.agentLaunchSelections[runtime.agentId] || {};
      replaceSelectOptions(routeSelect, managedRouteOptions(runtime.agentId), saved.route || runtime.route || runtime.model);
      routeLabel.append(routeSelect); controls.append(routeLabel);

      const permissionLabel = node("label", "compact-field"); permissionLabel.append(node("span", "", t("permission")));
      const permissionSelect = node("select", "agent-permission-select");
      const tiers = entry?.permission_tiers || [];
      tiers.forEach((tier) => {
        const option = node("option", "", `${permissionTierLabel(tier.tier_id)} · ${capabilityStatusLabel(tier.status)}`);
        option.value = tier.tier_id; option.disabled = !tier.launchable; permissionSelect.append(option);
      });
      const preferredPermission = saved.permission || runtime.permission || "observe";
      permissionSelect.value = Array.from(permissionSelect.options).some((option) => option.value === preferredPermission)
        ? preferredPermission
        : (Array.from(permissionSelect.options).find((option) => !option.disabled)?.value || "");
      permissionLabel.append(permissionSelect); controls.append(permissionLabel);
      const saveSelection = () => { state.agentLaunchSelections[runtime.agentId] = { route: routeSelect.value, permission: permissionSelect.value }; };
      routeSelect.addEventListener("change", saveSelection); permissionSelect.addEventListener("change", saveSelection);

      const action = node("button", "secondary-button", t(runtime.session ? "view_terminal" : "configure_agent")); action.type = "button";
      action.disabled = !runtime.session && !entry?.installed;
      action.addEventListener("click", () => {
        if (runtime.session) {
          state.selectedSessionId = runtime.session.session_id;
          state.cockpitMode = "focus";
          renderCockpit();
          byId("session-workspace").scrollIntoView({ behavior: "smooth", block: "start" });
          return;
        }
        saveSelection();
        byId("managed-agent").value = runtime.agentId;
        updateManagedRouteOptions(runtime.agentId, routeSelect.value);
        updateManagedPermissionControls();
        if (Array.from(byId("managed-permission").options).some((option) => option.value === permissionSelect.value)) {
          byId("managed-permission").value = permissionSelect.value;
          updateManagedPermissionControls();
        }
        byId("managed-launch-heading").scrollIntoView({ behavior: "smooth", block: "start" });
        byId("managed-input").focus();
      });
      controls.append(action); card.append(controls);
      container.append(card);
    });
  }

  function renderCockpit() {
    const cockpit = state.data?.cockpit || {}; const sessions = cockpit.sessions || []; const events = cockpit.events || []; const presence = cockpit.presence || [];
    if (!sessions.some((entry) => entry.session_id === state.selectedSessionId)) state.selectedSessionId = sessions[0]?.session_id || "";
    byId("cockpit-summary").textContent = `${sessions.length} ${t("managed_sessions")}`;
    renderMetricStrip("cockpit-metrics", [
      [t("managed_sessions"), sessions.length], [t("connected_agents"), presence.length], [t("session_activity"), events.length], [t("routes"), (state.data?.routes || []).length]
    ]);
    renderAgentRuntimeStrip();
    byId("session-count").textContent = String(sessions.length); byId("session-event-count").textContent = String(events.length);
    renderManagedControls();
    const sessionList = byId("session-list"); sessionList.replaceChildren();
    if (!sessions.length) {
      const dormantAgents = (state.data?.managed_agent_catalog || []).filter((entry) => entry.primary);
      if (!dormantAgents.length) sessionList.append(node("div", "panel-empty", t("no_records")));
      dormantAgents.forEach((entry) => {
        const card = node("article", "record-row session-row dormant-terminal");
        const head = node("div", "record-head");
        head.append(node("strong", "", entry.label || entry.agent_id), node("span", `status-badge ${entry.installed ? "muted" : "danger"}`, t(entry.installed ? "terminal_not_started" : "not_installed")));
        card.append(head, node("p", "", [entry.publisher, entry.version].filter(Boolean).join(" · ")));
        const dormantState = node("div", "dormant-terminal-state");
        dormantState.append(
          node("span", "presence-dot"),
          node("span", "", t("terminal_not_started"))
        );
        card.append(dormantState);
        const actions = node("div", "record-actions");
        const startButton = node("button", "primary-button", t("start_terminal")); startButton.type = "button"; startButton.disabled = !entry.installed;
        startButton.addEventListener("click", () => {
          byId("managed-agent").value = entry.agent_id;
          updateManagedRouteOptions(entry.agent_id);
          updateManagedPermissionControls();
          byId("managed-launch-heading").scrollIntoView({ behavior: "smooth", block: "start" });
          byId("managed-input").focus();
        });
        actions.append(startButton); card.append(actions); sessionList.append(card);
      });
    }
    sessions.forEach((entry) => {
      const statusValue = entry.state || entry.status || entry.lifecycle_state || "unknown";
      const row = node("article", `record-row session-row ${entry.session_id === state.selectedSessionId ? "selected" : ""}`);
      row.tabIndex = 0; row.setAttribute("role", "button"); row.setAttribute("aria-pressed", String(entry.session_id === state.selectedSessionId));
      const selectSession = () => { state.selectedSessionId = entry.session_id; renderCockpit(); };
      row.addEventListener("click", (event) => { if (!event.target.closest("button, input, select, textarea, label")) selectSession(); });
      row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectSession(); } });
      const head = node("div", "record-head"); head.append(node("strong", "", entry.display_name || entry.client_name || entry.session_id), node("span", `status-badge ${dispatchTone(statusValue)}`, displayValue(statusValue))); row.append(head);
      row.append(node("p", "", [entry.source_type, entry.adapter_id, entry.model_id || entry.requested_route].filter(Boolean).join(" · ")));
      const permissionEvidence = entry.permission_tier || entry.session_authorization?.permission_tier || "";
      const sessionRuntimeMeta = node("div", "session-runtime-meta");
      sessionRuntimeMeta.append(
        node("span", "", `${t("model")}: ${entry.model_id || entry.requested_route || "--"}`),
        node("span", "", `${t("permission")}: ${permissionEvidence ? permissionTierLabel(permissionEvidence) : t("unknown")}`)
      );
      const latestObservableEvent = latestByTime(events.filter((event) => event.session_id === entry.session_id), ["created_utc"]);
      const sessionFresh = Boolean(entry.managed) || recentObservable(
        entry,
        ["last_seen_utc", "updated_utc", "started_utc"],
        180000
      );
      const eventFresh = recentObservable(latestObservableEvent, ["created_utc"], 180000);
      const observable = terminalStatus(statusValue)
        ? { key: statusValue === "failed" ? "activity_failed" : "activity_completed", tone: statusValue === "failed" ? "danger" : "online" }
        : !sessionFresh
          ? { key: "activity_stale", tone: "offline" }
          : (latestObservableEvent && eventFresh ? eventActivity(latestObservableEvent) : { key: "activity_running", tone: "working" });
      const observableState = node("span", `agent-live-state ${observable.tone}`);
      observableState.append(node("i", "presence-dot"), node("span", "", t(observable.key)));
      sessionRuntimeMeta.append(observableState); row.append(sessionRuntimeMeta);
      const meta = node("div", "record-meta"); [entry.owner_agent_id, entry.role_id, timeLabel(entry.updated_utc || entry.last_seen_utc)].filter(Boolean).forEach((value) => meta.append(node("span", "", value))); row.append(meta);
      if (state.cockpitMode === "grid") {
        const recentEvents = events.filter((event) => event.session_id === entry.session_id).slice(0, 12).reverse();
        const terminal = node("pre", "session-terminal session-terminal-preview");
        const eventText = recentEvents.map((event) => {
          const stamp = timeLabel(event.created_utc) || "--:--";
          const channel = event.stream || event.kind || "event";
          return `[${stamp}] ${channel}\n${event.summary || event.state_after || "--"}`;
        }).join("\n\n");
        terminal.textContent = [entry.terminal_detail || entry.terminal_outcome || "", eventText].filter(Boolean).join("\n\n") || t("no_records");
        row.append(terminal);
      }
      const multimodal = entry.multimodal_capability || {};
      if (multimodal.attachment_input_supported) {
        const modes = [multimodal.image_input, multimodal.audio_input, multimodal.text_file_input].filter(Boolean).map(displayValue);
        row.append(node("p", "session-multimodal-summary", `${t("multimodal_input")} · ${modes.join(" · ")}`));
      }
      const receipts = Array.isArray(entry.attachment_delivery_receipts) ? entry.attachment_delivery_receipts : [];
      if (receipts.length) {
        const receipt = receipts[receipts.length - 1];
        const receiptText = [
          t("attachment_delivery"),
          t(String(receipt.status || "verified_path_available")),
          String(receipt.attachment_count || 0),
          receipt.model_view_confirmed === false ? t("model_view_not_confirmed") : ""
        ].filter(Boolean).join(" · ");
        row.append(node("p", "attachment-delivery-note", receiptText));
      }
      const visionReceipts = Array.isArray(entry.vision_verification_receipts) ? entry.vision_verification_receipts : [];
      const visionStatus = visionReceipts.length
        ? String(visionReceipts[visionReceipts.length - 1].status || "semantic_image_failed")
        : String(multimodal.semantic_image_verification || "");
      if (visionStatus && visionStatus !== "available") {
        row.append(node("p", `vision-verification-note ${visionStatus === "semantic_image_verified" ? "verified" : "unverified"}`, `${t("vision_verification")} · ${t(visionStatus)}`));
      }
      if (entry.managed) {
        const isTerminal = terminalStatus(statusValue);
        const contract = entry.session_contract || {};
        const pendingApprovals = Array.isArray(entry.approval_broker?.pending)
          ? entry.approval_broker.pending
          : [];
        pendingApprovals.forEach((approval) => {
          const card = node("section", `approval-request-card risk-${approval.risk || "elevated"}`);
          const heading = node("div", "approval-request-heading");
          heading.append(
            node("strong", "", approval.title || t("approval_waiting")),
            node("span", "status-badge warning", t("approval_waiting"))
          );
          card.append(heading);
          if (approval.detail) card.append(node("pre", "approval-request-detail", approval.detail));
          card.append(node("small", "", `${t("approval_risk")}: ${approval.risk || "--"}`));
          const actions = node("div", "approval-request-actions");
          const available = new Set(approval.available_decisions || []);
          const decide = async (decision) => {
            Array.from(actions.querySelectorAll("button")).forEach((button) => { button.disabled = true; });
            try {
              await postAction("/api/session/action", {
                session_id: entry.session_id,
                action: "approval",
                approval_id: approval.approval_id,
                approval_decision: decision
              });
              renderCockpit();
            } catch (error) {
              toast(`${t("action_failed")}: ${error.message}`);
              Array.from(actions.querySelectorAll("button")).forEach((button) => { button.disabled = false; });
            }
          };
          if (available.has("allow-once")) {
            const allowOnce = node("button", "primary-button", t("approval_allow_once"));
            allowOnce.type = "button";
            allowOnce.addEventListener("click", () => decide("allow-once"));
            actions.append(allowOnce);
          }
          if (available.has("allow-session")) {
            const allowSession = node("button", "secondary-button", t("approval_allow_session"));
            allowSession.type = "button";
            allowSession.addEventListener("click", () => decide("allow-session"));
            actions.append(allowSession);
          }
          const deny = node("button", "danger-button", t("approval_deny"));
          deny.type = "button";
          deny.addEventListener("click", () => decide("deny"));
          actions.append(deny);
          card.append(actions);
          row.append(card);
        });
        const persistent = entry.input_mode === "persistent" || Boolean(entry.session_contract?.additional_input_supported);
        const contractNote = node("p", "session-contract-note", persistent
          ? t("persistent_session_notice")
          : (entry.input_submitted ? t("one_shot_input_used") : t("one_shot_notice")));
        row.append(contractNote);
        if (entry.session_authorization?.mode === "once-per-session") {
          row.append(node(
            "p",
            "session-authorization-note",
            `${t("session_authorized_once")} · ${permissionTierLabel(entry.session_authorization.permission_tier)}`
          ));
        }
        const controls = node("div", `session-controls ${entry.can_submit_input ? "" : "actions-only"}`);
        controls.setAttribute("aria-label", t("native_actions"));
        let input = null; let send = null; let attachmentInput = null; let attachmentList = null; let attachmentTools = null; let visionTest = null;
        if (entry.can_submit_input) {
          input = node("input", "session-input"); input.type = "text"; input.maxLength = 20000; input.placeholder = t("managed_input_placeholder"); input.setAttribute("aria-label", t("send_to_session"));
          send = node("button", "secondary-button", t("send")); send.type = "button";
          if (multimodal.attachment_input_supported) {
            attachmentTools = node("div", "session-attachment-tools");
            attachmentInput = node("input", ""); attachmentInput.type = "file"; attachmentInput.multiple = true; attachmentInput.hidden = true;
            const allowedTurnSuffixes = managedSuffixesForCapability(multimodal);
            attachmentInput.accept = Array.from(allowedTurnSuffixes).join(",");
            const attachmentButton = node("button", "secondary-button", t("attach")); attachmentButton.type = "button";
            attachmentList = node("div", "attachment-list session-attachment-list");
            const renderTurnAttachments = () => {
              const rows = state.managedTurnAttachments[entry.session_id] || [];
              renderAttachmentCollection(attachmentList, rows, (index) => {
                rows.splice(index, 1);
                renderTurnAttachments();
              });
            };
            attachmentButton.addEventListener("click", () => attachmentInput.click());
            attachmentInput.addEventListener("change", async (event) => {
              const current = state.managedTurnAttachments[entry.session_id] || [];
              const next = await readManagedAttachmentFiles(event.target.files, current, allowedTurnSuffixes);
              if (next) state.managedTurnAttachments[entry.session_id] = next;
              attachmentInput.value = "";
              renderTurnAttachments();
            });
            input.addEventListener("paste", (event) => handleClipboardImages(event, async (files) => {
              const current = state.managedTurnAttachments[entry.session_id] || [];
              const next = await readManagedAttachmentFiles(files, current, allowedTurnSuffixes);
              if (!next) return false;
              state.managedTurnAttachments[entry.session_id] = next;
              renderTurnAttachments();
              return true;
            }));
            renderTurnAttachments();
            attachmentTools.append(attachmentInput, attachmentButton, attachmentList);
          }
          if (multimodal.attachment_input_supported && multimodal.image_input_supported !== false && multimodal.semantic_image_verification) {
            visionTest = node("button", "secondary-button", t("verify_vision"));
            visionTest.type = "button";
            visionTest.title = t("vision_test_hint");
          }
        }
        const actionButtons = [];
        const addActionButton = (action, label, className = "secondary-button", title = "") => {
          const button = node("button", className, label);
          button.type = "button";
          button.dataset.sessionAction = action;
          if (title) button.title = title;
          actionButtons.push(button);
          return button;
        };
        const review = contract.review_supported && entry.can_submit_input
          ? addActionButton("review", t("native_review"), "secondary-button", t("native_review_hint"))
          : null;
        const compact = contract.compact_supported && entry.can_submit_input
          ? addActionButton("compact", t("native_compact"))
          : null;
        const fork = contract.fork_supported && (entry.can_submit_input || isTerminal)
          ? addActionButton("fork", t("native_fork"))
          : null;
        const resume = contract.resume_supported && isTerminal
          ? addActionButton("resume", t("resume"))
          : null;
        const interrupt = contract.interrupt_supported && !isTerminal && !entry.can_submit_input
          ? addActionButton("interrupt", t("interrupt"))
          : null;
        const stop = !isTerminal ? addActionButton("stop", t("stop"), "danger-button") : null;
        const run = async (action) => {
          const textValue = ["send", "review"].includes(action) ? input?.value.trim() || "" : "";
          const turnAttachments = action === "send" ? (state.managedTurnAttachments[entry.session_id] || []) : [];
          if (action === "send" && !textValue && !turnAttachments.length) return;
          [send, visionTest, ...actionButtons].filter(Boolean).forEach((button) => { button.disabled = true; });
          try {
            await postAction("/api/session/action", {
              session_id: entry.session_id,
              action,
              input_text: textValue,
              attachments: turnAttachments.map(({ name, content_base64 }) => ({ name, content_base64 }))
            });
            if (["send", "review"].includes(action)) {
              if (input) input.value = "";
            }
            if (action === "send") {
              delete state.managedTurnAttachments[entry.session_id];
              if (attachmentList) attachmentList.replaceChildren();
            }
            await fetchState(true);
            renderCockpit();
            byId("managed-session-status").textContent = `${t("session_action_completed")}: ${t(action)}`;
          }
          catch (error) { toast(`${t("action_failed")}: ${error.message}`); }
          finally { [send, visionTest, ...actionButtons].filter(Boolean).forEach((button) => { button.disabled = false; }); }
        };
        if (send && input) {
          send.addEventListener("click", () => run("send"));
          input.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); run("send"); } });
          controls.append(input, send);
          if (attachmentTools) controls.append(attachmentTools);
        }
        if (visionTest) {
          visionTest.addEventListener("click", () => run("vision-test"));
          controls.append(visionTest);
        }
        [review, compact, fork, resume, interrupt, stop].filter(Boolean).forEach((button) => {
          button.addEventListener("click", () => run(button.dataset.sessionAction));
          controls.append(button);
        });
        if (controls.childElementCount) row.append(controls);
      }
      sessionList.append(row);
    });
    renderSessionDetails(sessions, events);
  }

  function renderOperations() {
    const rows = state.data?.operations || []; const schedules = state.data?.schedules || [];
    byId("operation-count").textContent = String(rows.length);
    renderMetricStrip("operation-metrics", [
      [t("operations"), rows.length], [t("active"), rows.filter((row) => ["running", "claimed", "active"].includes(String(row.status).toLowerCase())).length], [t("completed"), rows.filter((row) => ["completed", "done", "verified"].includes(String(row.status).toLowerCase())).length], [state.locale === "en" ? "Schedules" : state.locale === "zh-Hans" ? "计划" : "排程", schedules.length]
    ]);
    renderWorkflowControls();
    const list = byId("operation-list"); list.replaceChildren();
    if (!rows.length) list.append(node("div", "panel-empty", t("no_records")));
    rows.forEach((entry) => {
      const row = node("article", "record-row operation-row"); const head = node("div", "record-head");
      head.append(node("strong", "", entry.task_text || entry.workflow_id || entry.operation_id), node("span", `status-badge ${dispatchTone(entry.status)}`, displayValue(entry.status))); row.append(head);
      if (entry.terminal_detail || entry.terminal_outcome) row.append(node("p", "", entry.terminal_detail || entry.terminal_outcome));
      const meta = node("div", "record-meta"); [workflowLabel({ workflow_id: entry.workflow_id }), entry.requested_by, `${t("attempt")} ${entry.attempt_count || 0}/${entry.max_attempts || 0}`, timeLabel(entry.updated_utc)].filter(Boolean).forEach((value) => meta.append(node("span", "", value))); row.append(meta);
      if (!terminalStatus(entry.status) && !entry.cancellation_requested) {
        const actions = node("div", "record-actions"); const cancel = node("button", "danger-button", t("cancel_operation")); cancel.type = "button";
        cancel.addEventListener("click", async () => { cancel.disabled = true; try { await postAction("/api/operation/cancel", { operation_id: entry.operation_id, reason: "Operator cancelled from Workbench" }); toast(t("operation_cancelled")); } catch (error) { toast(`${t("action_failed")}: ${error.message}`); } finally { cancel.disabled = false; } });
        actions.append(cancel); row.append(actions);
      }
      list.append(row);
    });
    const scheduleList = byId("schedule-list"); byId("schedule-count").textContent = String(schedules.length); scheduleList.replaceChildren();
    if (!schedules.length) scheduleList.append(node("div", "panel-empty", t("no_records")));
    schedules.forEach((entry) => {
      const row = node("article", "record-row schedule-row"); const head = node("div", "record-head");
      head.append(node("strong", "", entry.schedule_id), node("span", `status-badge ${entry.enabled ? "success" : "muted"}`, t(entry.enabled ? "enabled" : "disabled"))); row.append(head);
      row.append(node("p", "", entry.task_text || "--"));
      const meta = node("div", "record-meta");
      [workflowLabel({ workflow_id: entry.workflow_id }), `${Math.max(1, Math.round(Number(entry.interval_seconds || 60) / 60))} ${t("interval_minutes")}`, entry.next_run_epoch ? `${t("schedule_next_run")}: ${epochLabel(entry.next_run_epoch)}` : "", timeLabel(entry.updated_utc), (entry.sha256 || "").slice(0, 12)].filter(Boolean).forEach((value) => meta.append(node("span", "", value))); row.append(meta);
      const actions = node("div", "record-actions"); const edit = node("button", "secondary-button", t("edit_schedule")); edit.type = "button"; const toggle = node("button", "secondary-button", t(entry.enabled ? "disable" : "enable")); toggle.type = "button";
      edit.addEventListener("click", () => {
        byId("schedule-id").value = entry.schedule_id || "";
        byId("schedule-workflow").value = entry.workflow_id || byId("schedule-workflow").value;
        byId("schedule-task").value = entry.task_text || "";
        byId("schedule-interval").value = String(Math.max(1, Math.round(Number(entry.interval_seconds || 60) / 60)));
        byId("schedule-delay").value = "0";
        byId("schedule-enabled").checked = Boolean(entry.enabled);
        byId("schedule-form").scrollIntoView({ behavior: "smooth", block: "center" });
        byId("schedule-task").focus();
      });
      toggle.addEventListener("click", async () => {
        toggle.disabled = true;
        try { await postAction("/api/schedule/enabled", { schedule_id: entry.schedule_id, enabled: !entry.enabled }); toast(t(entry.enabled ? "schedule_disabled" : "schedule_enabled")); }
        catch (error) { toast(`${t("action_failed")}: ${error.message}`); }
        finally { toggle.disabled = false; }
      });
      actions.append(edit, toggle); row.append(actions); scheduleList.append(row);
    });
  }

  const permissionTierLabel = (tierId) => t(`tier_${String(tierId || "observe").replaceAll("-", "_")}`);
  const permissionTierHint = (tierId) => t(`permission_hint_${String(tierId || "observe").replaceAll("-", "_")}`);
  const capabilityLabel = (capabilityId) => t(`capability_${capabilityId}`);
  const adapterCapabilityLabel = (capabilityId) => (
    (adapterCapabilityLabels[state.locale] || adapterCapabilityLabels.en)[capabilityId]
    || capabilityId
  );
  const adapterCapabilityStateLabel = (status) => t(`adapter_state_${status || "unsupported"}`);
  const capabilityStatusLabel = (status) => t(`status_${String(status || "not_verified")}`);
  const capabilityTone = (status) => {
    if (new Set(["verified", "supported"]).has(status)) return "success";
    if (status === "configured") return "active";
    if (new Set(["gated", "conditional"]).has(status)) return "warning";
    if (new Set(["unavailable", "unsupported"]).has(status)) return "danger";
    return "muted";
  };

  function renderOfficialAgentCards() {
    const container = byId("official-agent-grid");
    if (!container) return;
    container.replaceChildren();
    const agents = (state.data?.managed_agent_catalog || []).filter((entry) => entry.primary);
    if (!agents.length) {
      container.append(node("div", "panel-empty", t("no_records")));
      return;
    }
    agents.forEach((entry) => {
      const card = node("article", "official-agent-card");
      const heading = node("div", "official-agent-heading");
      const identity = node("div", "official-agent-identity");
      identity.append(node("span", "official-agent-mark", initials(entry.label || entry.agent_id)));
      const copy = node("div", "");
      copy.append(node("strong", "", entry.label || entry.agent_id), node("small", "", entry.publisher || "--"));
      identity.append(copy);
      heading.append(identity, node("span", `status-badge ${entry.installed ? "success" : "danger"}`, t(entry.installed ? "installed" : "not_installed")));
      card.append(heading);
      if (entry.automatic_install_supported) {
        const installActions = node("div", "official-agent-actions");
        const installButton = node("button", "secondary-button", t(entry.installed ? "update_agent" : "install_agent"));
        installButton.type = "button";
        installButton.addEventListener("click", async () => {
          if (!window.confirm(t("install_agent_confirm"))) return;
          installButton.disabled = true;
          try {
            await postAction("/api/agent/install", { agent_id: entry.agent_id, confirmed: true, update: Boolean(entry.installed) });
            toast(t("agent_installer_started"));
          } catch (error) { toast(`${t("action_failed")}: ${error.message}`); }
          finally { installButton.disabled = false; }
        });
        installActions.append(installButton);
        card.append(installActions);
      } else if (!entry.installed && entry.docs_url) {
        const installActions = node("div", "official-agent-actions");
        const guide = node("a", "secondary-button", t("publisher_guide"));
        guide.href = entry.docs_url; guide.target = "_blank"; guide.rel = "noopener noreferrer";
        installActions.append(guide); card.append(installActions);
      }

      const receipt = entry.receipt || null;
      const meta = node("div", "official-agent-meta");
      meta.append(node("span", "", `${t("client_version")}: ${entry.version || receipt?.observed_version || "--"}`));
      meta.append(node("span", "", `${t("observed_model")}: ${receipt?.observed_model || "--"}`));
      meta.append(node("span", `verification-label ${receipt?.real_inference_verified ? "verified" : ""}`, t(receipt?.real_inference_verified ? "local_e2e_verified" : "no_local_receipt")));
      card.append(meta);

      const nativeContract = entry.native_contract || {};
      const nativeAxis = node("section", "capability-axis native-contract-axis");
      nativeAxis.append(node("h4", "capability-axis-title", t("native_client_contract")));
      const contractGrid = node("dl", "native-contract-grid");
      const contractRows = [
        [t("transport"), t(`transport_${nativeContract.transport || "direct_official_cli"}`)],
        [t("session_mode"), t(nativeContract.managed_session_mode === "persistent" ? "session_persistent" : "session_one_shot")],
        [t("input_transport"), nativeContract.input_transport ? t(`input_transport_${nativeContract.input_transport}`) : t("input_stdin_once")],
        [t("read_only_profile"), t(nativeContract.read_only_profile_ready ? "mapped_yes" : "mapped_no")],
        [t("model_route_configurable"), t(nativeContract.model_route_configurable ? "mapped_yes" : "mapped_no")],
        [t("session_resume_mapped"), t(nativeContract.resume_mapped ? "mapped_yes" : "mapped_no")]
      ];
      contractRows.forEach(([label, value]) => {
        contractGrid.append(node("dt", "", label), node("dd", "", value));
      });
      nativeAxis.append(contractGrid);
      card.append(nativeAxis);

      const adapterCapabilities = entry.adapter?.capabilities || [];
      if (adapterCapabilities.length) {
        const adapterAxis = node("section", "capability-axis adapter-capability-axis");
        adapterAxis.append(node("h4", "capability-axis-title", t("adapter_capabilities")));
        const adapterRows = node("div", "capability-list");
        adapterCapabilities.forEach((capability) => {
          const capabilityRow = node("div", "capability-row");
          const label = node("span", "capability-name", adapterCapabilityLabel(capability.capability_id));
          label.title = [capability.evidence, capability.limitation].filter(Boolean).join(" · ");
          capabilityRow.append(
            label,
            node(
              "span",
              `status-badge ${capabilityTone(capability.state)}`,
              adapterCapabilityStateLabel(capability.state)
            )
          );
          adapterRows.append(capabilityRow);
        });
        adapterAxis.append(adapterRows);
        card.append(adapterAxis);
      }

      const tiers = node("div", "permission-tier-strip");
      (entry.permission_tiers || []).forEach((tier) => {
        const chip = node("span", `permission-tier-chip ${capabilityTone(tier.status)}`);
        chip.append(node("strong", "", permissionTierLabel(tier.tier_id)), node("small", "", capabilityStatusLabel(tier.status)));
        tiers.append(chip);
      });
      card.append(tiers);

      const mappingAxis = node("section", "capability-axis peerbridge-mapping-axis");
      mappingAxis.append(node("h4", "capability-axis-title", t("peerbridge_verified_mapping")));
      const capabilities = node("div", "capability-list");
      (entry.peerbridge_mappings || entry.capabilities || []).forEach((capability) => {
        const row = node("div", "capability-row");
        row.append(
          node("span", "capability-name", capabilityLabel(capability.capability_id)),
          node("span", `status-badge ${capabilityTone(capability.status)}`, capabilityStatusLabel(capability.status))
        );
        capabilities.append(row);
      });
      mappingAxis.append(capabilities);
      card.append(mappingAxis);
      container.append(card);
    });
    const acpxRequired = agents.some((entry) => new Set(["grok", "kimi-code"]).has(entry.agent_id) && !entry.observe_dependencies_ready);
    if (acpxRequired) {
      const card = node("article", "official-agent-card dependency-card");
      const heading = node("div", "official-agent-heading");
      const identity = node("div", "official-agent-identity");
      identity.append(node("span", "official-agent-mark", "AX"));
      const copy = node("div", ""); copy.append(node("strong", "", t("acpx_runtime")), node("small", "", t("acpx_required"))); identity.append(copy);
      heading.append(identity, node("span", "status-badge danger", t("not_installed"))); card.append(heading);
      const actions = node("div", "official-agent-actions"); const install = node("button", "secondary-button", t("install_dependency")); install.type = "button";
      install.addEventListener("click", async () => {
        if (!window.confirm(t("install_agent_confirm"))) return;
        install.disabled = true;
        try { await postAction("/api/agent/install", { agent_id: "acpx-runtime", confirmed: true, update: false }); toast(t("agent_installer_started")); }
        catch (error) { toast(`${t("action_failed")}: ${error.message}`); }
        finally { install.disabled = false; }
      });
      actions.append(install); card.append(actions); container.append(card);
    }
  }

  function updateManagedRouteOptions(agentId = byId("managed-agent").value, selectedValue = "") {
    replaceSelectOptions(byId("managed-route"), managedRouteOptions(agentId), selectedValue || byId("managed-route").value);
  }

  function updateManagedPermissionControls() {
    const agents = state.data?.managed_agent_catalog || [];
    const entry = agents.find((candidate) => candidate.agent_id === byId("managed-agent").value);
    const select = byId("managed-permission");
    const prior = select.value;
    select.replaceChildren();
    (entry?.permission_tiers || []).forEach((tier) => {
      const option = node("option", "", `${permissionTierLabel(tier.tier_id)} · ${capabilityStatusLabel(tier.status)}`);
      option.value = tier.tier_id;
      option.disabled = !tier.launchable;
      select.append(option);
    });
    const launchable = (entry?.permission_tiers || []).filter((tier) => tier.launchable);
    const selected = launchable.find((tier) => tier.tier_id === prior) || launchable[0] || entry?.permission_tiers?.[0];
    select.value = selected?.tier_id || "";
    byId("managed-permission-hint").textContent = permissionTierHint(selected?.tier_id || "observe");
    const bindingField = byId("managed-binding-field");
    const bindingSelect = byId("managed-binding");
    const requiresBinding = Boolean(selected?.requires_governance_binding);
    const bindings = (state.data?.executions || []).filter((row) => row.agent_id === entry?.agent_id && row.state === "active");
    replaceSelectOptions(
      bindingSelect,
      bindings.map((row) => [row.binding_id, `${row.task_id} · ${String(row.sha256 || "").slice(0, 10)}`]),
      bindingSelect.value
    );
    bindingField.hidden = !requiresBinding;
    bindingSelect.required = requiresBinding;
    const directory = byId("managed-directory");
    directory.disabled = requiresBinding;
    if (requiresBinding) directory.value = ".";
    const ready = Boolean(entry?.installed && selected?.launchable && (!requiresBinding || bindings.length));
    byId("managed-start").disabled = !ready;
    const status = byId("managed-session-status");
    if (!ready) {
      status.textContent = entry?.installed
        ? (requiresBinding && !bindings.length ? t("no_governed_worktree") : capabilityStatusLabel(selected?.status))
        : t("not_installed");
      status.dataset.catalogState = "true";
    } else if (status.dataset.catalogState === "true") {
      status.textContent = "";
      delete status.dataset.catalogState;
    }
  }

  function renderManagedControls() {
    const agents = state.data?.managed_agent_catalog || [];
    const agentSelect = byId("managed-agent");
    replaceSelectOptions(agentSelect, agents.map((entry) => [entry.agent_id, entry.label || entry.agent_id]), agentSelect.value);
    replaceSelectOptions(byId("managed-role"), managedRoleValues().map((value) => [value, roleLabel(value)]), byId("managed-role").value || "equal-participant");
    updateManagedRouteOptions(agentSelect.value);
    renderOfficialAgentCards();
    updateManagedPermissionControls();
    agentSelect.onchange = () => { updateManagedRouteOptions(agentSelect.value); updateManagedPermissionControls(); renderOfficialAgentCards(); };
    byId("managed-permission").onchange = updateManagedPermissionControls;
    byId("managed-binding").onchange = updateManagedPermissionControls;
  }

  function renderWorkflowControls() {
    const templates = state.data?.workflow_templates || [];
    const select = byId("workflow-template"); const prior = select.value;
    replaceSelectOptions(select, templates.map((entry) => [entry.workflow_id, workflowLabel(entry)]), prior);
    const selected = templates.find((entry) => entry.workflow_id === select.value);
    if (selected && !byId("workflow-attempts").dataset.touched) byId("workflow-attempts").value = selected.automatic_retry ? "3" : "1";
    byId("workflow-enqueue").disabled = !templates.length;
    replaceSelectOptions(byId("schedule-workflow"), templates.map((entry) => [entry.workflow_id, workflowLabel(entry)]), byId("schedule-workflow")?.value);
  }

  function renderReviews() {
    const calls = state.data?.peer_calls || []; const reviews = state.data?.peer_reviews || [];
    byId("review-summary").textContent = `${calls.length} / ${reviews.length}`; byId("peer-call-count").textContent = String(calls.length); byId("peer-review-count").textContent = String(reviews.length);
    renderRecordList("peer-call-list", calls, (entry) => ({
      title: entry.question || entry.request_id,
      status: entry.status,
      body: [[entry.requester, "→", entry.recipient].filter(Boolean).join(" "), entry.response].filter(Boolean).join("\n"),
      meta: [entry.task_id, entry.approval_mode, `${entry.artifact_count || 0}/${entry.response_artifact_count || 0} ${t("files")}`, (entry.request_sha256 || "").slice(0, 16), (entry.response_sha256 || "").slice(0, 16), timeLabel(entry.request_utc), timeLabel(entry.response_utc)]
    }));
    renderRecordList("peer-review-list", reviews, (entry) => ({
      title: entry.findings || entry.review_id,
      status: entry.verdict,
      body: entry.reviewer,
      meta: [entry.task_id, entry.score === null || entry.score === undefined ? "" : `${entry.score}`, `${entry.artifact_count || 0} ${t("files")}`, (entry.sha256 || "").slice(0, 16), timeLabel(entry.review_utc)]
    }));
  }

  function renderWorktreeDiff() {
    const summary = byId("worktree-diff-summary");
    const files = byId("worktree-diff-files");
    const viewer = byId("worktree-diff-view");
    if (!summary || !files || !viewer) return;
    files.replaceChildren(); viewer.replaceChildren();
    if (state.worktreeDiffLoading) {
      summary.textContent = "…";
      viewer.append(node("div", "panel-empty", "…"));
      return;
    }
    const diff = state.worktreeDiff;
    if (!diff) {
      summary.textContent = "--";
      viewer.append(node("div", "panel-empty", t("diff_unavailable")));
      return;
    }
    if (!diff.available) {
      summary.textContent = t("diff_unavailable");
      viewer.append(node("div", "panel-empty", t("diff_unavailable")));
      return;
    }
    summary.textContent = `${diff.file_count || 0} ${t("code_files")} · +${diff.additions || 0} / −${diff.deletions || 0}`;
    (diff.files || []).forEach((entry) => {
      const row = node("div", "diff-file-row");
      row.append(node("span", `diff-file-status status-${String(entry.status || "m").toLowerCase()}`, entry.status || "M"));
      row.append(node("span", "diff-file-path", entry.path || "--"));
      const stat = node("span", "diff-file-stat");
      if (entry.binary) stat.textContent = "BIN";
      else {
        stat.append(node("span", "diff-stat-add", `+${entry.additions ?? "?"}`), node("span", "diff-stat-delete", `−${entry.deletions ?? "?"}`));
      }
      row.append(stat); files.append(row);
    });
    if (!(diff.files || []).length) files.append(node("div", "panel-empty", t("diff_clean")));
    if (!diff.patch) {
      viewer.append(node("div", "panel-empty", diff.dirty ? t("diff_unavailable") : t("diff_clean")));
      return;
    }
    const code = node("div", "diff-code");
    const patchLines = String(diff.patch).split("\n");
    const visiblePatchLines = patchLines.slice(0, MAX_DIFF_RENDER_LINES);
    visiblePatchLines.forEach((line) => {
      let className = "diff-line";
      if (line.startsWith("diff --git") || line.startsWith("index ")) className += " diff-file-header";
      else if (line.startsWith("@@")) className += " diff-hunk";
      else if (line.startsWith("+") && !line.startsWith("+++")) className += " diff-add";
      else if (line.startsWith("-") && !line.startsWith("---")) className += " diff-delete";
      else if (line.startsWith("+++ ") || line.startsWith("--- ")) className += " diff-path";
      code.append(node("div", className, line || " "));
    });
    viewer.append(code);
    if (diff.patch_truncated || diff.files_truncated || patchLines.length > visiblePatchLines.length) {
      viewer.append(node("div", "diff-truncated", t("diff_truncated")));
    }
  }

  async function fetchWorktreeDiff(force = false) {
    if (state.worktreeDiffLoading || (!force && state.worktreeDiff)) return;
    state.worktreeDiffLoading = true; renderWorktreeDiff();
    try {
      const response = await fetchWithTimeout("/api/worktree/diff", {
        headers: { Authorization: authorizationValue() },
        cache: "no-store"
      }, 15000);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || String(response.status));
      state.worktreeDiff = payload;
    } catch (error) {
      state.worktreeDiff = { available: false, reason: localizedErrorMessage(error.message), files: [], patch: "" };
    } finally {
      state.worktreeDiffLoading = false; renderWorktreeDiff();
    }
  }

  function renderChanges() {
    const rows = state.data?.changes || []; byId("change-summary").textContent = `${rows.length} ${t("records")}`;
    byId("recorded-change-count").textContent = String(rows.length);
    renderWorktreeDiff();
    renderRecordList("change-list", rows, (entry) => ({
      title: entry.summary || entry.record_id,
      status: entry.approval_mode,
      body: [entry.test_summary, ...(entry.changed_paths || [])].filter(Boolean).join("\n"),
      meta: [entry.actor, entry.task_id, `${entry.changed_path_count || 0} ${t("files")}`, `${entry.review_count || 0} ${t("review_results")}`, (entry.sha256 || "").slice(0, 16), timeLabel(entry.recorded_utc)]
    }));
  }

  function renderAudit() {
    byId("snapshot-signature").textContent = (state.data?.signature || "").slice(0, 16);
    const grid = byId("audit-grid"); grid.replaceChildren();
    Object.entries(state.data?.counts || {}).forEach(([key, value]) => { const item = node("div", "audit-item"); item.append(node("span", "", t(key)), node("strong", "", compact(value))); grid.append(item); });
    if (!grid.childElementCount) grid.append(node("div", "panel-empty", t("no_records")));
    const rows = state.data?.events || []; byId("audit-event-count").textContent = String(rows.length);
    renderRecordList("audit-event-list", rows, (entry) => ({
      title: entry.event_type || entry.event_id,
      body: entry.actor,
      meta: [entry.task_id, entry.sequence, timeLabel(entry.created_utc), (entry.payload_sha256 || "").slice(0, 12), (entry.prev_chain_sha256 || "").slice(0, 12), (entry.chain_sha256 || "").slice(0, 16)]
    }));
  }

  function renderTrust() {
    const trust = state.data?.trust || [];
    const permissions = state.data?.permissions || [];
    const capabilities = state.data?.capabilities || [];
    const grants = state.data?.capability_grants || [];
    const executions = state.data?.executions || [];
    const total = trust.length + permissions.length + capabilities.length + grants.length + executions.length;
    byId("trust-summary").textContent = `${total} ${t("records")}`;
    byId("trust-count").textContent = String(trust.length);
    byId("permission-count").textContent = String(permissions.length);
    byId("capability-count").textContent = String(capabilities.length);
    byId("capability-grant-count").textContent = String(grants.length);
    byId("execution-count").textContent = String(executions.length);
    renderRecordList("trust-list", trust, (entry) => ({
      title: entry.statement || entry.record_id,
      status: entry.stage,
      body: entry.actor,
      meta: [entry.task_id, `${entry.source_count || 0} ${t("evidence")}`, timeLabel(entry.created_utc)]
    }));
    renderRecordList("permission-list", permissions, (entry) => ({
      title: entry.action || entry.decision_id,
      status: entry.decision,
      body: entry.reason,
      meta: [entry.agent_id, entry.task_id, entry.decided_by, entry.expires_epoch ? `${t("expires")}: ${epochLabel(entry.expires_epoch)}` : "", entry.consumed_utc ? `${t("consumed")}: ${timeLabel(entry.consumed_utc)}` : "", (entry.sha256 || "").slice(0, 16), timeLabel(entry.created_utc)]
    }));
    renderRecordList("capability-list", capabilities, (entry) => ({
      title: entry.display_name || entry.capability_id,
      status: entry.enabled ? t("enabled") : t("disabled"),
      body: [entry.kind, entry.sensitivity].filter(Boolean).join(" · "),
      meta: [entry.capability_id, entry.registry_version, entry.registered_by, (entry.sha256 || "").slice(0, 16), timeLabel(entry.created_utc)]
    }));
    renderRecordList("capability-grant-list", grants, (entry) => ({
      title: entry.capability_id || entry.grant_id,
      status: entry.decision,
      body: entry.reason,
      meta: [[entry.principal_type, entry.principal_id].filter(Boolean).join(":"), entry.decided_by, (entry.sha256 || "").slice(0, 16), timeLabel(entry.created_utc)]
    }));
    const executionList = byId("execution-list"); executionList.replaceChildren();
    if (!executions.length) executionList.append(node("div", "panel-empty", t("no_records")));
    executions.forEach((entry) => {
      const row = node("article", "record-row execution-row");
      const head = node("div", "record-head");
      head.append(node("strong", "", entry.binding_id || "--"), node("span", `status-badge ${dispatchTone(entry.state)}`, displayValue(entry.state)));
      row.append(head, node("p", "", [entry.task_id, entry.agent_id].filter(Boolean).join(" · ")));
      const meta = node("div", "record-meta");
      [entry.permission_decision_id, (entry.base_commit_sha256 || "").slice(0, 16), (entry.final_commit_sha256 || "").slice(0, 16), timeLabel(entry.updated_utc)].filter(Boolean).forEach((value) => meta.append(node("span", "", value)));
      row.append(meta);
      const actions = node("div", "record-actions");
      const seal = node("button", "secondary-button", t("seal")); seal.type = "button";
      const verify = node("button", "secondary-button", t("verify")); verify.type = "button";
      const run = async (action) => {
        seal.disabled = true; verify.disabled = true;
        try {
          await postAction(`/api/execution/${action}`, { binding_id: entry.binding_id });
          toast(t(action === "seal" ? "execution_sealed" : "execution_verified"));
        } catch (error) { toast(`${t("action_failed")}: ${error.message}`); }
        finally { seal.disabled = false; verify.disabled = false; }
      };
      seal.addEventListener("click", () => run("seal")); verify.addEventListener("click", () => run("verify"));
      const executionState = String(entry.state || "").toLowerCase();
      if (!new Set(["sealed", "verified"]).has(executionState)) actions.append(seal);
      if (new Set(["sealed", "verified"]).has(executionState)) actions.append(verify);
      if (actions.childElementCount) row.append(actions); executionList.append(row);
    });
  }

  function renderConnections() {
    const connections = state.data?.connections || []; const routes = state.data?.routes || [];
    const enabledConnections = connections.filter((entry) => entry.enabled);
    byId("connect-summary").textContent = `${connections.filter((row) => row.enabled).length} / ${connections.length}`; byId("connection-count").textContent = String(connections.length); byId("route-count").textContent = String(routes.length);
    replaceSelectOptions(
      byId("provider-route-connection"),
      enabledConnections.length
        ? enabledConnections.map((entry) => [entry.connection_id, `${entry.display_name || entry.connection_id} · ${entry.route_class || "--"}`])
        : [["", t("no_records")]]
    );
    renderRecordList("connection-list", connections, (entry) => ({
      title: entry.display_name || entry.connection_id,
      status: entry.enabled ? t("enabled") : t("disabled"),
      body: [entry.provider_id, entry.route_class].filter(Boolean).join(" · "),
      meta: [timeLabel(entry.updated_utc), (entry.endpoint_sha256 || "").slice(0, 12)]
    }));
    renderRecordList("route-list", routes, (entry) => ({
      title: [entry.provider_id, entry.model_id].filter(Boolean).join(" / ") || entry.route_id,
      status: entry.enabled ? t("enabled") : t("disabled"),
      body: [entry.agent_id, entry.client_name, entry.reasoning_mode].filter(Boolean).join(" · "),
      meta: [entry.route_class, entry.timeout_seconds ? `${entry.timeout_seconds}s` : "", timeLabel(entry.updated_utc)]
    }));
  }

  function renderMemory() {
    const memories = state.data?.memories || []; const briefings = state.data?.briefings || [];
    byId("memory-summary").textContent = `${memories.length} ${t("memory_records")}`; byId("memory-count").textContent = String(memories.length); byId("briefing-count").textContent = String(briefings.length);
    renderRecordList("memory-list", memories, (entry) => ({
      title: entry.title || entry.memory_id,
      status: entry.status,
      body: entry.body,
      meta: [entry.record_type, entry.visibility, entry.owner_agent_id, timeLabel(entry.created_utc)]
    }));
    renderRecordList("briefing-list", briefings, (entry) => ({
      title: entry.task_id || entry.briefing_id,
      body: [entry.agent_id, ...(entry.memory_bindings || []).map((binding) => `${binding.record_type || "--"} · ${binding.memory_id || "--"} · ${(binding.memory_sha256 || "").slice(0, 12)}`)].filter(Boolean).join("\n"),
      meta: [entry.room_id, `${entry.memory_count || 0} ${t("memory_records")}`, (entry.sha256 || "").slice(0, 16), timeLabel(entry.created_utc)]
    }));
  }

  function renderUsageTrend(rows) {
    const target = byId("usage-trend"); target.replaceChildren();
    if (!rows.length) { target.append(node("div", "panel-empty", t("no_records"))); return; }
    const metrics = [
      ["input_tokens", t("input"), "var(--blue)"],
      ["output_tokens", t("output"), "var(--accent)"],
      ["cached_input_tokens", t("cache"), "var(--green)"],
      ["reasoning_tokens", t("reasoning"), "var(--amber)"]
    ];
    const width = 720; const height = 230; const left = 42; const right = 14; const top = 18; const bottom = 34;
    const values = rows.flatMap((row) => metrics.map(([key]) => row[key] === null || row[key] === undefined ? null : Number(row[key]))).filter((value) => Number.isFinite(value));
    const maximum = Math.max(1, ...values);
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`); svg.setAttribute("role", "img"); svg.setAttribute("aria-label", t("token_trend"));
    for (let index = 0; index <= 4; index += 1) {
      const y = top + ((height - top - bottom) * index / 4);
      const line = document.createElementNS(svg.namespaceURI, "line"); line.setAttribute("x1", String(left)); line.setAttribute("x2", String(width - right)); line.setAttribute("y1", String(y)); line.setAttribute("y2", String(y)); line.setAttribute("class", "usage-grid-line"); svg.append(line);
    }
    metrics.forEach(([key, label, color]) => {
      const segments = []; let segment = [];
      rows.forEach((row, index) => {
        if (row[key] === null || row[key] === undefined || !Number.isFinite(Number(row[key]))) {
          if (segment.length) segments.push(segment);
          segment = [];
          return;
        }
        const x = left + ((width - left - right) * (rows.length === 1 ? 0.5 : index / (rows.length - 1)));
        const y = top + (height - top - bottom) * (1 - (Number(row[key]) / maximum));
        segment.push(`${x.toFixed(2)},${y.toFixed(2)}`);
      });
      if (segment.length) segments.push(segment);
      segments.forEach((points) => { const path = document.createElementNS(svg.namespaceURI, "polyline"); path.setAttribute("points", points.join(" ")); path.setAttribute("fill", "none"); path.setAttribute("stroke", color); path.setAttribute("stroke-width", "2"); path.setAttribute("vector-effect", "non-scaling-stroke"); path.setAttribute("aria-label", label); svg.append(path); });
    });
    [0, Math.floor((rows.length - 1) / 2), rows.length - 1].filter((value, index, values) => values.indexOf(value) === index).forEach((index) => {
      const x = left + ((width - left - right) * (rows.length === 1 ? 0.5 : index / (rows.length - 1)));
      const label = document.createElementNS(svg.namespaceURI, "text"); label.setAttribute("x", String(x)); label.setAttribute("y", String(height - 10)); label.setAttribute("text-anchor", index === 0 ? "start" : index === rows.length - 1 ? "end" : "middle"); label.setAttribute("class", "usage-axis-label"); label.textContent = rows[index].period_label || rows[index].period_key || "--"; svg.append(label);
    });
    const legend = node("div", "usage-trend-legend"); metrics.forEach(([, label, color]) => { const item = node("span", ""); const mark = node("i", ""); mark.style.background = color; item.append(mark, document.createTextNode(label)); legend.append(item); });
    target.append(svg, legend);
  }

  function renderUsage() {
    const usage = state.data?.usage || {}; const periods = usage.periods || {};
    if (!periods[state.usagePeriod]) state.usagePeriod = periods["30d"] ? "30d" : periods.all ? "all" : Object.keys(periods)[0] || "all";
    const selected = periods[state.usagePeriod] || usage.totals || {};
    const periodLabels = { today: t("period_today"), "7d": t("period_7d"), "30d": t("period_30d"), all: t("period_all") };
    const selector = byId("usage-periods"); selector.replaceChildren();
    Object.entries(periodLabels).forEach(([key, label]) => {
      const row = periods[key] || {}; const button = node("button", `usage-period-button${key === state.usagePeriod ? " active" : ""}`); button.type = "button"; button.setAttribute("role", "tab"); button.setAttribute("aria-selected", String(key === state.usagePeriod)); button.append(node("span", "", label), node("strong", "", row.total_tokens === null || row.total_tokens === undefined ? "--" : compact(row.total_tokens)), node("small", "", `${row.reported_calls === null || row.reported_calls === undefined ? "--" : compact(row.reported_calls)} ${state.locale === "en" ? "calls" : state.locale === "zh-Hans" ? "次调用" : "次呼叫"}`)); button.addEventListener("click", () => { state.usagePeriod = key; renderUsage(); }); selector.append(button);
    });
    const values = [
      [t("input"), selected.input_tokens === null || selected.input_tokens === undefined ? null : Number(selected.input_tokens)], [t("output"), selected.output_tokens === null || selected.output_tokens === undefined ? null : Number(selected.output_tokens)], [t("cache"), selected.cached_input_tokens === null || selected.cached_input_tokens === undefined ? null : Number(selected.cached_input_tokens)], [t("reasoning"), selected.reasoning_tokens === null || selected.reasoning_tokens === undefined ? null : Number(selected.reasoning_tokens)]
    ];
    const max = Math.max(1, ...values.map((row) => row[1]).filter((value) => Number.isFinite(value))); const bars = byId("usage-bars"); bars.replaceChildren();
    values.forEach(([label, value]) => { const row = node("div", "usage-row"); const track = node("div", "bar-track"); const fill = node("div", "bar-fill"); fill.style.width = `${Number.isFinite(value) ? Math.max(0, (value / max) * 100) : 0}%`; track.append(fill); row.append(node("span", "", label), track, node("strong", "usage-value", Number.isFinite(value) ? compact(value) : "--")); bars.append(row); });
    byId("usage-total").textContent = `${periodLabels[state.usagePeriod] || ""} · ${t("total")} ${selected.total_tokens === null || selected.total_tokens === undefined ? "--" : compact(selected.total_tokens)}`;
    const providers = byId("usage-providers"); providers.replaceChildren(); (selected.providers || []).forEach((provider) => { const row = node("div", "usage-model"); row.append(node("strong", "", provider.provider_id || "--"), node("span", "", `${provider.provider_calls === null || provider.provider_calls === undefined ? "--" : compact(provider.provider_calls)} ${state.locale === "en" ? "calls" : state.locale === "zh-Hans" ? "次调用" : "次呼叫"}`), node("span", "", provider.total_tokens === null || provider.total_tokens === undefined ? "--" : compact(provider.total_tokens))); providers.append(row); }); if (!providers.childElementCount) providers.append(node("div", "panel-empty", t("no_records")));
    const models = byId("usage-models"); models.replaceChildren(); (selected.models || []).forEach((model) => { const row = node("div", "usage-model"); row.append(node("strong", "", model.model_id || "--"), node("span", "", model.provider_id || "--"), node("span", "", model.total_tokens === null || model.total_tokens === undefined ? "--" : compact(model.total_tokens))); models.append(row); }); if (!models.childElementCount) models.append(node("div", "panel-empty", t("no_records")));
    byId("usage-truncation").hidden = !selected.trend_truncated;
    byId("usage-truncation").textContent = selected.trend_truncated ? t("usage_truncated") : "";
    renderUsageTrend(selected.trend || []);
  }

  function renderSupport() {
    const feedback = state.data?.feedback || {};
    const feedbackReady = feedback.configured && !feedback.configuration_error;
    byId("feedback-summary").textContent = feedbackReady ? t("enabled") : t("disabled");
    byId("feedback-heading").textContent = feedbackReady ? t("feedback_ready") : t("feedback_unavailable");
    byId("feedback-detail").textContent = feedbackReady ? t("feedback_body") : t("feedback_unavailable");
    const feedbackBadge = byId("feedback-service-status");
    feedbackBadge.textContent = feedback.delivery_configured ? t("online") : feedbackReady ? t("enabled") : t("disabled");
    feedbackBadge.className = `status-badge ${feedbackReady ? "success" : "danger"}`;
    byId("feedback-submit").disabled = !feedbackReady;
    const encryptionReady = Boolean(feedback.encrypted_credential_available);
    byId("feedback-credential").disabled = !encryptionReady;
    byId("feedback-credential-consent").disabled = !encryptionReady;
    byId("feedback-credential-toggle").disabled = !encryptionReady;
    byId("feedback-credential").placeholder = t(encryptionReady ? "feedback_credential_placeholder" : "feedback_encryption_unavailable");

    const announcements = state.data?.announcement_state || {};
    const announcementReady = announcements.configured && !announcements.configuration_error;
    byId("announcement-summary").textContent = announcementReady ? t("enabled") : t("disabled");
    byId("announcement-detail").textContent = !announcementReady ? t("announcement_unconfigured") : announcements.network_enabled ? t("announcement_ready") : t("announcement_disabled");
    byId("refresh-announcements").disabled = !announcementReady || !announcements.network_enabled;
    const rows = announcements.announcements || [];
    const unread = rows.filter((entry) => !entry.read).length;
    const unreadBadge = byId("announcement-unread");
    const announcementButton = byId("announcement-button");
    byId("mark-announcements-read").disabled = unread === 0;
    unreadBadge.hidden = unread === 0;
    unreadBadge.textContent = "";
    unreadBadge.title = unread ? `${t("announcement")} · ${unread}` : "";
    announcementButton.setAttribute("aria-label", unread ? `${t("announcement")} (${unread})` : t("announcement"));
    const list = byId("announcement-list"); list.replaceChildren();
    if (!rows.length) list.append(node("div", "panel-empty", t("no_announcements")));
    rows.forEach((entry) => {
      const row = node("article", `record-row announcement-row severity-${entry.severity || "info"}`);
      const head = node("div", "record-head"); head.append(node("strong", "", entry.title || "--"), node("span", `status-badge ${dispatchTone(entry.severity)}`, entry.severity || "info"));
      row.append(head, node("p", "", entry.body || ""));
      const meta = node("div", "record-meta"); meta.append(node("span", "", timeLabel(entry.published_utc)));
      if (entry.link_url) { const link = node("a", "announcement-link", state.locale === "en" ? "Open" : state.locale === "zh-Hans" ? "查看" : "查看"); link.href = entry.link_url; link.target = "_blank"; link.rel = "noopener noreferrer"; meta.append(link); }
      row.append(meta); list.append(row);
    });
  }

  function renderRound() {
    const panel = byId("round-summary"); panel.replaceChildren(); const auto = state.data?.automation || {}; const discussion = auto.active_discussion;
    [[t("status"), discussion?.status || automationText(auto.mode)], [t("round"), discussion ? `${discussion.round_index} / ${auto.max_rounds}` : "--"], [t("messages"), discussion?.message_count ?? "--"]].forEach(([label, value]) => { const card = node("div", "round-card"); card.append(node("span", "", label), node("strong", "", String(value))); panel.append(card); });
    byId("work-mode").textContent = discussion?.status || automationText(auto.mode);

    const dispatches = state.data?.dispatches || []; byId("dispatch-count").textContent = String(dispatches.length);
    const dispatchList = byId("dispatch-list"); dispatchList.replaceChildren();
    if (!dispatches.length) dispatchList.append(node("div", "panel-empty", t("no_dispatches")));
    dispatches.slice(0, 14).forEach((dispatch) => {
      const row = node("div", "dispatch-row");
      const copy = node("div", "dispatch-copy"); copy.append(node("strong", "", dispatch.agent_id || "Agent"), node("span", "", dispatch.message_id.slice(0, 14)));
      const status = node("span", `status-badge ${dispatchTone(dispatch.status)}`, dispatchLabel(dispatch.status));
      row.append(copy, status); dispatchList.append(row);
    });

    const tasks = state.data?.tasks || []; byId("work-task-count").textContent = String(tasks.length);
    const taskList = byId("work-task-list"); taskList.replaceChildren();
    tasks.slice(0, 8).forEach((task) => {
      const row = node("div", "work-task-row");
      row.append(node("strong", "", task.task_id), node("span", "", task.summary || task.status || "--"));
      const meta = node("div", "work-task-meta"); meta.append(node("span", `status-badge ${dispatchTone(task.status)}`, task.status || "--"), node("span", "", task.claimed_by || "--")); row.append(meta); taskList.append(row);
    });
  }

  function completedTaskCount(tasks) {
    const completed = new Set(["complete", "completed", "done", "verified", "closed", "passed"]);
    return tasks.filter((task) => completed.has(String(task.status || "").toLowerCase())).length;
  }

  function renderActivity() {
    const members = state.data?.members || [];
    const tasks = state.data?.tasks || [];
    const online = members.filter((member) => member.online).length;
    const completed = completedTaskCount(tasks);
    const mode = state.data?.automation?.mode || "off";
    const discussion = state.data?.automation?.active_discussion;
    byId("overview-title").textContent = discussion ? t("discussion_active") : mode === "once" ? t("one_round_ready") : t("room_ready");
    const contextPolicy = state.data?.context_policy || {};
    const contextDetail = contextPolicy.enabled
      ? `${t("context_active")} · ${contextPolicy.max_messages} ${t("messages")}`
      : t("activity_hint");
    byId("overview-detail").textContent = discussion?.termination_reason || contextDetail;
    byId("overview-agents").textContent = `${online} / ${members.length}`;
    byId("overview-tasks").textContent = `${completed} / ${tasks.length}`;
    const dispatches = state.data?.dispatches || [];
    const dispatched = dispatches.filter((row) => row.status === "completed").length;
    byId("overview-dispatches").textContent = `${dispatched} / ${dispatches.length}`;
    byId("overview-events").textContent = compact(state.data?.counts?.events || 0);
  }

  function renderEvidence() {
    const panel = byId("evidence-summary"); panel.replaceChildren();
    const rows = [
      [t("evidence_snapshot"), (state.data?.signature || "").slice(0, 16)],
      [t("messages"), `${compact(state.data?.counts?.messages || 0)} ${t("records")}`],
      [t("tasks"), `${compact(state.data?.counts?.tasks || 0)} ${t("records")}`],
      [t("audit_events"), `${compact(state.data?.counts?.events || 0)} ${t("records")}`]
    ];
    rows.forEach(([label, value]) => {
      const row = node("div", "evidence-row");
      row.append(node("span", "", label), node("strong", "", value));
      panel.append(row);
    });
    const updates = state.data?.work_updates || []; byId("work-update-count").textContent = String(updates.length);
    const updateList = byId("work-update-list"); updateList.replaceChildren();
    if (!updates.length) updateList.append(node("div", "panel-empty", t("no_updates")));
    updates.slice(0, 12).forEach((update) => {
      const row = node("div", "work-update-row");
      const head = node("div", "work-update-head"); head.append(node("strong", "", update.agent_id || "Agent"), node("span", "", timeLabel(update.created_utc)));
      row.append(head, node("p", "", update.summary || "--"));
      const meta = node("div", "work-update-meta"); meta.append(node("span", "", update.task_id), node("span", "", update.status || "--"));
      if (update.artifact_count) meta.append(node("span", "", `${update.artifact_count} ${t("files")}`)); row.append(meta); updateList.append(row);
    });
  }

  function render() {
    if (!state.data) return;
    document.querySelectorAll(".content-view").forEach((el) => el.classList.toggle("active-view", el.id === `${state.view}-view`));
    document.querySelectorAll(".nav-item").forEach((el) => el.classList.toggle("active", el.dataset.view === state.view));
    byId("chat-home").hidden = state.view === "chat";
    byId("announcement-button").classList.toggle("active", state.view === "announcement");
    const room = state.data.rooms.find((row) => row.room_id === state.data.room_id);
    byId("room-title").textContent = room?.name || state.data.room_id; byId("room-context").textContent = `${state.data.scope} · ${state.data.members.length} ${t("agents")}`; byId("scope-label").textContent = state.data.scope; byId("scope-chip").textContent = state.data.scope; byId("header-scope").textContent = state.data.scope;
    byId("appearance-current").textContent = state.data?.appearance?.selected === "pixel" ? "Pixel" : "Modern";
    const historyRecord = state.data.history_import?.selected || null;
    const historyNotice = byId("history-notice"); historyNotice.hidden = !historyRecord;
    if (historyRecord) {
      const collapsed = Number(state.data?.page?.collapsed_duplicate_count || 0);
      const duplicateNotice = collapsed > 0
        ? ` · ${t("history_duplicates_collapsed")}: ${collapsed}`
        : "";
      byId("history-detail").textContent = `${historyRecord.provider} · ${historyRecord.source_conversation_id}${duplicateNotice}`;
      byId("history-hash").textContent = String(historyRecord.source_sha256 || "").slice(0, 16);
    }
    const automation = state.data.automation || {};
    byId("automation-label").textContent = automationText(automation.mode); byId("room-message-count").textContent = compact(room?.message_count || 0); byId("room-token-count").textContent = compact(state.data.usage?.totals?.total_tokens || 0);
    byId("automation-mode").value = automation.mode || "off"; byId("automation-rounds").value = automation.max_rounds || 4; byId("automation-messages").value = automation.max_messages || 40; byId("automation-stagnation").value = automation.stagnation_rounds || 2;
    byId("discussion-controls").hidden = !automation.active_discussion;
    ["send-button", "message-body", "message-subject", "attachment-button", "recipient", "composer-permission", "priority", "save-automation"].forEach((id) => { byId(id).disabled = !state.data.operator_active; });
    if (state.data.operator_active) updateComposerPermissionControls();
    byId("message-body").placeholder = state.data.operator_active ? t("composer_placeholder") : historyRecord ? t("history_read_only") : t("no_operator");
    renderRooms(); renderMessages(); renderAgents(); renderTasks(); renderCockpit(); renderOperations(); renderReviews(); renderChanges(); renderAudit(); renderTrust(); renderConnections(); renderMemory(); renderUsage(); renderSupport(); renderRound(); renderActivity(); renderEvidence();
  }

  async function fetchState(force = false) {
    if (!state.token || state.loading) return; state.loading = true;
    try {
      const headers = { Authorization: authorizationValue() }; if (!force && state.etag) headers["If-None-Match"] = state.etag;
      const response = await fetchWithTimeout(
        `/api/bootstrap?room_id=${encodeURIComponent(state.roomId)}`,
        { headers, cache: "no-store" },
        15000
      );
      if (response.status === 304) { setConnection(true); return; }
      if (response.status === 401) {
        sessionStorage.removeItem(workbenchSessionStorageKey);
        state.token = "";
        window.clearInterval(state.timer);
        byId("app").hidden = true;
        byId("access-gate").hidden = false;
        return;
      }
      if (!response.ok) throw new Error(String(response.status)); const payload = await response.json();
      state.etag = response.headers.get("ETag") || ""; state.roomId = payload.room_id; const changed = payload.signature !== state.signature; state.data = payload; state.signature = payload.signature;
      if (!state.tutorialAutoChecked) {
        state.tutorialAutoChecked = true;
        const savedLocale = payload.appearance?.locale;
        if (!localStorage.getItem("peerbridge.locale") && savedLocale in translations) {
          state.locale = savedLocale;
          localStorage.setItem("peerbridge.locale", state.locale);
          applyLocale();
        }
        if (payload.appearance?.tutorial_completed === false) {
          window.setTimeout(openTutorial, 120);
        }
      }
      if (changed || force) render(); setConnection(true); byId("connection-error").hidden = true;
    } catch (error) {
      setConnection(false);
      const panel = byId("connection-error");
      panel.textContent = `${t("bootstrap_failed")}${error?.message ? ` · ${localizedErrorMessage(error.message)}` : ""}`;
      panel.hidden = false;
    }
    finally { state.loading = false; }
  }

  function renderAttachments() {
    const list = byId("attachment-list"); list.replaceChildren();
    state.attachments.forEach((attachment, index) => {
      const chip = node("span", "attachment-chip"); chip.append(node("span", "", `${attachment.name} · ${compact(attachment.size)}B`));
      const remove = node("button", "", "×"); remove.type = "button"; remove.title = t("remove"); remove.setAttribute("aria-label", `${t("remove")}: ${attachment.name}`);
      remove.addEventListener("click", () => { state.attachments.splice(index, 1); renderAttachments(); }); chip.append(remove); list.append(chip);
    });
  }

  function readAttachment(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader(); reader.onerror = () => reject(new Error(file.name));
      reader.onload = () => resolve({ name: file.name, size: file.size, content_base64: String(reader.result || "").split(",", 2)[1] || "" });
      reader.readAsDataURL(file);
    });
  }

  function clipboardImageFiles(event) {
    const clipboard = event?.clipboardData;
    if (!clipboard) return [];
    let files = Array.from(clipboard.files || []).filter((file) => String(file.type || "").toLowerCase().startsWith("image/"));
    if (!files.length) {
      files = Array.from(clipboard.items || [])
        .filter((item) => item.kind === "file" && String(item.type || "").toLowerCase().startsWith("image/"))
        .map((item) => item.getAsFile())
        .filter(Boolean);
    }
    const extensions = { "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp" };
    return files.map((file, index) => {
      const lowerName = String(file.name || "").toLowerCase();
      if (Array.from(managedImageAttachmentSuffixes).some((suffix) => lowerName.endsWith(suffix))) return file;
      const suffix = extensions[String(file.type || "").toLowerCase()] || ".png";
      return new File([file], `clipboard-image-${Date.now()}-${index + 1}${suffix}`, { type: file.type || "image/png", lastModified: file.lastModified || Date.now() });
    });
  }

  async function handleClipboardImages(event, addFiles) {
    const files = clipboardImageFiles(event);
    if (!files.length) return false;
    event.preventDefault();
    const added = await addFiles(files);
    if (added !== false) toast(t("clipboard_image_attached"));
    return true;
  }

  function renderAttachmentCollection(list, attachments, onRemove) {
    list.replaceChildren();
    attachments.forEach((attachment, index) => {
      const chip = node("span", "attachment-chip");
      chip.append(node("span", "", `${attachment.name} · ${compact(attachment.size)}B`));
      const remove = node("button", "", "×"); remove.type = "button"; remove.title = t("remove"); remove.setAttribute("aria-label", `${t("remove")}: ${attachment.name}`);
      remove.addEventListener("click", () => onRemove(index));
      chip.append(remove); list.append(chip);
    });
  }

  function attachmentValidationError(selected, currentRows, allowedSuffixes = null) {
    if (allowedSuffixes && selected.some((file) => {
      const index = file.name.lastIndexOf(".");
      return !allowedSuffixes.has(index >= 0 ? file.name.slice(index).toLowerCase() : "");
    })) return "attachment_type_invalid";
    if (currentRows.length + selected.length > 5) return "attachment_count_limit";
    if (selected.some((file) => file.size > 8 * 1024 * 1024)) return "attachment_file_size_limit";
    if ([...currentRows, ...selected].reduce((sum, file) => sum + Number(file.size || 0), 0) > 16 * 1024 * 1024) return "attachment_total_size_limit";
    return "";
  }

  async function readManagedAttachmentFiles(files, currentRows = [], allowedSuffixes = managedAttachmentSuffixes) {
    const selected = Array.from(files || []); if (!selected.length) return null;
    const validationError = attachmentValidationError(selected, currentRows, allowedSuffixes);
    if (validationError) { toast(t(validationError)); return null; }
    try { return [...currentRows, ...await Promise.all(selected.map(readAttachment))]; }
    catch (error) { toast(`${t("action_failed")}: ${error.message}`); return null; }
  }

  function renderManagedAttachments() {
    renderAttachmentCollection(byId("managed-attachment-list"), state.managedAttachments, (index) => {
      state.managedAttachments.splice(index, 1);
      renderManagedAttachments();
    });
  }

  async function addManagedAttachments(files) {
    const next = await readManagedAttachmentFiles(files, state.managedAttachments);
    if (next) state.managedAttachments = next;
    byId("managed-attachment-input").value = "";
    renderManagedAttachments();
    return Boolean(next);
  }

  async function addAttachments(files) {
    const selected = Array.from(files || []); if (!selected.length) return false;
    const validationError = attachmentValidationError(selected, state.attachments, managedAttachmentSuffixes);
    if (validationError) { toast(t(validationError)); return false; }
    try { state.attachments.push(...await Promise.all(selected.map(readAttachment))); renderAttachments(); return true; }
    catch (error) { toast(`${t("action_failed")}: ${error.message}`); return false; }
    finally { byId("attachment-input").value = ""; }
  }

  function renderFeedbackAttachments() {
    const list = byId("feedback-attachment-list"); list.replaceChildren();
    state.feedbackAttachments.forEach((attachment, index) => {
      const chip = node("span", "attachment-chip");
      chip.append(node("span", "", `${attachment.name} · ${compact(attachment.size)}B`));
      const remove = node("button", "", "×"); remove.type = "button"; remove.title = t("remove"); remove.setAttribute("aria-label", `${t("remove")}: ${attachment.name}`);
      remove.addEventListener("click", () => { state.feedbackAttachments.splice(index, 1); renderFeedbackAttachments(); });
      chip.append(remove); list.append(chip);
    });
  }

  async function addFeedbackAttachments(files) {
    const selected = Array.from(files || []); if (!selected.length) return false;
    const allowed = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".json", ".log", ".txt"]);
    const validationError = attachmentValidationError(selected, state.feedbackAttachments, allowed);
    if (validationError) { toast(t(validationError)); return false; }
    try { state.feedbackAttachments.push(...await Promise.all(selected.map(readAttachment))); renderFeedbackAttachments(); return true; }
    catch (error) { toast(`${t("action_failed")}: ${error.message}`); return false; }
    finally { byId("feedback-attachment-input").value = ""; }
  }

  async function submitFeedback(event) {
    event.preventDefault();
    const summary = byId("feedback-summary-input").value.trim();
    const message = byId("feedback-message").value.trim();
    const credentialInput = byId("feedback-credential");
    const consentInput = byId("feedback-credential-consent");
    let credential = credentialInput.value;
    const consent = consentInput.checked;
    const encryptionReady = Boolean(state.data?.feedback?.encrypted_credential_available);
    if (!summary || !message) { toast(t("feedback_required")); return; }
    if (credential && !encryptionReady) { toast(t("feedback_encryption_unavailable")); return; }
    if (credential && !consent) { toast(t("feedback_consent_required")); return; }
    const button = byId("feedback-submit"); const status = byId("feedback-status");
    button.disabled = true; status.textContent = t("feedback_submitting");
    let requestBody = "";
    try {
      let payload = {
        request_id: makeRequestId(), summary, message,
        contact: byId("feedback-contact").value.trim(), locale: state.locale,
        credential_input: credential, include_encrypted_credential: Boolean(credential && consent),
        attachments: state.feedbackAttachments.map(({ name, content_base64 }) => ({ name, content_base64 }))
      };
      requestBody = JSON.stringify(payload);
      payload.credential_input = ""; payload = null; credential = "";
      credentialInput.value = ""; consentInput.checked = false;
      const response = await fetch("/api/feedback", { method: "POST", headers: { Authorization: authorizationValue(), "Content-Type": "application/json" }, body: requestBody });
      const result = await response.json().catch(() => ({})); if (!response.ok) throw new Error(result.error || String(response.status));
      const label = result.delivered ? t("feedback_delivered") : t("feedback_saved");
      status.textContent = `${label}: ${result.case_id || "--"}`; toast(status.textContent);
      byId("feedback-summary-input").value = ""; byId("feedback-message").value = ""; byId("feedback-contact").value = "";
      state.feedbackAttachments = []; renderFeedbackAttachments();
    } catch (error) { status.textContent = `${t("action_failed")}: ${error.message}`; toast(status.textContent); }
    finally { requestBody = ""; credential = ""; button.disabled = !(state.data?.feedback?.configured && !state.data?.feedback?.configuration_error); }
  }

  async function refreshAnnouncements() {
    const button = byId("refresh-announcements"); button.disabled = true;
    try {
      const response = await fetch("/api/announcements/refresh", { method: "POST", headers: { Authorization: authorizationValue(), "Content-Type": "application/json" }, body: JSON.stringify({ request_id: makeRequestId(), locale: state.locale }) });
      const result = await response.json().catch(() => ({})); if (!response.ok) throw new Error(result.error || String(response.status));
      state.etag = ""; await fetchState(true); toast(`${t("announcement_updated")}: ${result.received || 0}`);
    } catch (error) { toast(`${t("action_failed")}: ${error.message}`); }
    finally { button.disabled = !(state.data?.announcement_state?.configured && state.data?.announcement_state?.network_enabled); }
  }

  async function markAnnouncementsRead() {
    const button = byId("mark-announcements-read"); button.disabled = true;
    try {
      await postAction("/api/announcements/read", { locale: state.locale });
      toast(t("announcements_marked_read"));
    } catch (error) { toast(`${t("action_failed")}: ${error.message}`); }
    finally { button.disabled = false; }
  }

  async function verifyAuditChain() {
    const button = byId("audit-verify"); const status = byId("audit-verify-status"); button.disabled = true; status.textContent = "…";
    try {
      await postAction("/api/audit/verify", {});
      status.textContent = t("audit_chain_verified"); toast(t("audit_chain_verified"));
    } catch (error) { status.textContent = `${t("action_failed")}: ${error.message}`; toast(status.textContent); }
    finally { button.disabled = false; }
  }

  function openReviewWorkflow() {
    state.view = "work";
    render();
    byId("workflow-template").value = "implement-review";
    renderWorkflowControls();
    byId("workflow-task").focus();
    byId("work-view").scrollTo({ top: 0, left: 0, behavior: "auto" });
    closeMobilePanels();
  }

  function prepareManagedTaskFromComposer(body, approvalMode) {
    const recipient = byId("recipient").value;
    if (recipient === "*") {
      toast(t("managed_permission_requires_agent"));
      return false;
    }
    const managed = managedAgentForRoomAgent(recipient);
    if (!managed?.installed) {
      toast(t("managed_permission_unavailable"));
      return false;
    }
    state.view = "cockpit";
    state.preparedApprovalMode = approvalMode;
    render();
    byId("managed-agent").value = managed.agent_id;
    const saved = state.agentLaunchSelections[managed.agent_id] || {};
    updateManagedRouteOptions(managed.agent_id, saved.route || "");
    const permissionTier = approvalMode === "full-access" ? "full-development" : "edit";
    if (Array.from(byId("managed-permission").options).some(
      (option) => option.value === permissionTier && !option.disabled
    )) {
      byId("managed-permission").value = permissionTier;
    }
    updateManagedPermissionControls();
    byId("managed-input").value = body;
    state.managedAttachments = state.attachments.map((entry) => ({ ...entry }));
    renderManagedAttachments();
    byId("managed-launch-heading").scrollIntoView({ behavior: "auto", block: "start" });
    byId("managed-input").focus();
    toast(t("managed_permission_prepared"));
    return true;
  }

  async function sendMessage(event) {
    event.preventDefault(); const typedBody = byId("message-body").value.trim(); const body = typedBody || (state.attachments.length ? t("attachment_only_message") : ""); if (!body || !state.data?.operator_active) return;
    const approvalMode = byId("composer-permission").value;
    const directManaged = byId("recipient").value !== "*"
      && managedAgentForRoomAgent(byId("recipient").value)?.installed;
    if (approvalMode !== "approval-required" || directManaged) {
      prepareManagedTaskFromComposer(body, approvalMode);
      return;
    }
    const button = byId("send-button"); button.disabled = true; byId("send-status").textContent = "…";
    try {
      const fallbackSubject = state.locale === "en" ? "Human intervention" : state.locale === "zh-Hans" ? "人工介入" : "人工介入";
      const payload = {
        request_id: makeRequestId(), room_id: state.data.room_id, recipient: byId("recipient").value,
        task_id: `human-chat-${new Date().toISOString().slice(0,10).replaceAll("-", "")}`,
        subject: byId("message-subject").value.trim() || fallbackSubject, body, priority: byId("priority").value,
        attachments: state.attachments.map(({ name, content_base64 }) => ({ name, content_base64 }))
      };
      const response = await fetch("/api/message", { method: "POST", headers: { Authorization: authorizationValue(), "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      const result = await response.json(); if (!response.ok) throw new Error(result.error || String(response.status));
      byId("message-body").value = ""; byId("message-subject").value = ""; state.attachments = []; renderAttachments(); byId("send-status").textContent = t("sent"); state.etag = ""; await fetchState(true);
    } catch (error) { byId("send-status").textContent = t("send_failed"); toast(`${t("send_failed")}: ${error.message}`); }
    finally { button.disabled = !state.data?.operator_active; window.setTimeout(() => { byId("send-status").textContent = ""; }, 2500); }
  }

  function clamp(value, minimum, maximum) { return Math.min(maximum, Math.max(minimum, value)); }

  function setPanelSize(property, value, minimum, maximum) {
    const size = clamp(Math.round(value), minimum, maximum);
    document.documentElement.style.setProperty(property, `${size}px`);
    localStorage.setItem(`peerbridge.workbench${property}`, String(size));
    return size;
  }

  function restorePanelSizes() {
    [["--sidebar", 190, 320], ["--inspector", 260, 420]].forEach(([property, minimum, maximum]) => {
      const stored = Number(localStorage.getItem(`peerbridge.workbench${property}`));
      if (Number.isFinite(stored) && stored > 0) setPanelSize(property, stored, minimum, maximum);
    });
  }

  function bindPanelResize(handleId, panelId, property, minimum, maximum, direction) {
    const handle = byId(handleId); const panel = byId(panelId);
    const updateAria = () => handle.setAttribute("aria-valuenow", String(Math.round(panel.getBoundingClientRect().width)));
    handle.addEventListener("pointerdown", (event) => {
      if (window.matchMedia("(max-width: 1120px)").matches) return;
      event.preventDefault(); handle.setPointerCapture(event.pointerId); handle.classList.add("dragging"); document.body.classList.add("panel-dragging");
      const startX = event.clientX; const startWidth = panel.getBoundingClientRect().width;
      const move = (moveEvent) => { setPanelSize(property, startWidth + ((moveEvent.clientX - startX) * direction), minimum, maximum); updateAria(); };
      const end = () => { handle.classList.remove("dragging"); document.body.classList.remove("panel-dragging"); handle.removeEventListener("pointermove", move); handle.removeEventListener("pointerup", end); handle.removeEventListener("pointercancel", end); };
      handle.addEventListener("pointermove", move); handle.addEventListener("pointerup", end); handle.addEventListener("pointercancel", end);
    });
    handle.addEventListener("keydown", (event) => {
      if (!new Set(["ArrowLeft", "ArrowRight", "Home", "End"]).has(event.key)) return;
      event.preventDefault(); const current = panel.getBoundingClientRect().width;
      const next = event.key === "Home" ? minimum : event.key === "End" ? maximum : current + ((event.key === "ArrowRight" ? 12 : -12) * direction);
      setPanelSize(property, next, minimum, maximum); updateAria();
    });
    updateAria();
  }

  function closeMobilePanels() { byId("sidebar").classList.remove("open"); byId("inspector").classList.remove("open"); }

  function startHistoryContinuation() {
    if (!state.data?.history_import?.selected) return;
    state.historyContinuationSourceRoom = state.data.room_id;
    const sourceName = state.data.rooms.find((room) => room.room_id === state.data.room_id)?.name || t("imported_room");
    byId("new-room-id").value = `continue-${Date.now().toString(36)}`;
    byId("new-room-name").value = `${sourceName} · ${t("continue_history")}`.slice(0, 120);
    const dialog = byId("new-room-dialog");
    if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "");
    byId("new-room-name").focus();
  }

  async function createRoom(event) {
    event.preventDefault(); const roomId = byId("new-room-id").value.trim(); const name = byId("new-room-name").value.trim();
    if (!roomId || !name) return;
    const button = byId("new-room-submit"); button.disabled = true;
    try {
      const sourceRoomId = state.historyContinuationSourceRoom;
      await postAction(sourceRoomId ? "/api/history/continue" : "/api/room/create", sourceRoomId ? { source_room_id: sourceRoomId, room_id: roomId, name } : { room_id: roomId, name });
      state.roomId = roomId; state.older = []; state.signature = ""; state.firstRender = true; state.etag = "";
      state.historyContinuationSourceRoom = ""; byId("new-room-dialog").close(); byId("new-room-form").reset(); await fetchState(true); toast(t(sourceRoomId ? "history_continued" : "room_created"));
    } catch (error) { toast(`${t("action_failed")}: ${error.message}`); }
    finally { button.disabled = false; }
  }

  function openAppearanceDialog() {
    const selected = state.data?.appearance?.selected || "modern";
    const radio = document.querySelector(`input[name="appearance-surface"][value="${selected}"]`);
    if (radio) radio.checked = true;
    byId("appearance-status").textContent = "";
    const dialog = byId("appearance-dialog");
    if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "");
  }

  async function saveAppearance(event) {
    event.preventDefault();
    const selected = document.querySelector('input[name="appearance-surface"]:checked')?.value;
    if (!selected) return;
    const button = event.currentTarget.querySelector('button[type="submit"]');
    button.disabled = true;
    byId("appearance-status").textContent = "…";
    try {
      const response = await postAction("/api/appearance/save", { surface: selected });
      if (state.data) state.data.appearance = { ...(state.data.appearance || {}), selected: response.selected || selected };
      byId("appearance-current").textContent = selected === "pixel" ? "Pixel" : "Modern";
      byId("appearance-status").textContent = t("appearance_saved");
      toast(t("appearance_saved"));
    } catch (error) {
      byId("appearance-status").textContent = `${t("action_failed")}: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function importHistory(event) {
    event.preventDefault();
    const file = byId("history-file").files?.[0];
    const provider = byId("history-provider").value;
    const selectedSessions = [...byId("native-history-list").querySelectorAll('input[type="checkbox"]:checked')].map((input) => input.value);
    const status = byId("history-import-status");
    const button = byId("history-submit");
    if (selectedSessions.length > 20) {
      status.textContent = t("history_selection_limit"); toast(status.textContent); return;
    }
    if (!selectedSessions.length) {
      status.textContent = t("history_selection_required");
      toast(status.textContent);
      return;
    }
    button.disabled = true; status.textContent = t("history_importing");
    try {
      let result;
      if (!file) {
        const imported = [];
        for (const selectionHandle of selectedSessions) {
          imported.push(provider === "codex"
            ? await postAction("/api/history/codex/import", { selection_handle: selectionHandle }, { timeoutMs: 120000 })
            : await postAction("/api/history/native/import", { provider, selection_handle: selectionHandle }, { timeoutMs: 120000 }));
        }
        result = { room_id: imported[0].room_id, imported_count: imported.length };
      } else {
        result = await (async () => {
            const content = await readAttachment(file);
            return postAction("/api/history/import", {
              provider,
              source_name: content.name,
              content_base64: content.content_base64,
              selection_handles: selectedSessions
            }, { timeoutMs: 120000 });
          })();
      }
      state.roomId = result.room_id; state.older = []; state.signature = ""; state.firstRender = true; state.etag = "";
      byId("history-dialog").close(); byId("history-form").reset(); status.textContent = "";
      await fetchState(true); state.view = "chat"; render(); toast(t("history_imported"));
    } catch (error) {
      status.textContent = `${t("action_failed")}: ${error.message}`; toast(status.textContent);
    } finally { updateHistorySubmitState(); }
  }

  function updateHistorySubmitState() {
    const selected = byId("native-history-list").querySelectorAll(
      'input[type="checkbox"]:checked'
    ).length;
    byId("history-submit").disabled = selected === 0;
  }

  function updateHistorySourceControls() {
    const provider = byId("history-provider").value;
    const direct = byId("native-history-direct");
    const list = byId("native-history-list");
    direct.hidden = false;
    byId("native-history-title").textContent = `${provider === "claude" ? "Claude" : provider === "grok" ? "Grok" : provider === "kimi" ? "Kimi" : provider === "generic" ? t("history_generic") : "Codex"} · ${t("local_conversations")}`;
    byId("native-history-hint").textContent = provider === "codex" ? t("codex_direct_history_hint") : provider === "generic" ? t("history_import_hint") : t("native_direct_history_hint");
    list.hidden = true;
    list.replaceChildren();
    updateHistorySubmitState();
  }

  async function discoverNativeHistory() {
    const provider = byId("history-provider").value;
    const button = byId("native-history-discover");
    const status = byId("history-import-status");
    const list = byId("native-history-list");
    button.disabled = true; status.textContent = t("history_discovering");
    try {
      const file = byId("history-file").files?.[0];
      let result;
      if (file) {
        const content = await readAttachment(file);
        result = await postAction("/api/history/file/discover", {
          provider,
          source_name: content.name,
          content_base64: content.content_base64
        }, { timeoutMs: 60000 });
      } else {
        if (provider === "generic") throw new Error(t("history_invalid_file"));
        result = provider === "codex"
          ? await postAction("/api/history/codex/discover", {}, { timeoutMs: 60000 })
          : await postAction("/api/history/native/discover", { provider }, { timeoutMs: 60000 });
      }
      const threads = result.threads || result.sessions || result.conversations || [];
      list.replaceChildren();
      threads.forEach((row) => {
        const selectionHandle = row.selection_handle;
        const label = node("label", "history-selection-row");
        const checkbox = node("input"); checkbox.type = "checkbox"; checkbox.value = selectionHandle;
        checkbox.addEventListener("change", updateHistorySubmitState);
        const copy = node("span", "history-selection-copy");
        copy.append(node("strong", "", row.title || row.source_ref), node("span", "", `${timeLabel(row.updated_utc || row.ended_utc) || "--"} · ${row.source || provider}`));
        label.append(checkbox, copy); list.append(label);
      });
      list.hidden = !threads.length;
      updateHistorySubmitState();
      status.textContent = threads.length ? `${t("history_discovered")}: ${threads.length}` : t("no_history_found");
    } catch (error) {
      status.textContent = `${t("action_failed")}: ${error.message}`; toast(status.textContent);
    } finally { button.disabled = false; }
  }

  async function addSeat() {
    const button = byId("seat-add"); const agentId = byId("seat-agent").value; if (!agentId || !state.data) { toast(t("no_available_agent")); return; }
    if (state.data.history_import?.selected) return toast(t("history_read_only"));
    button.disabled = true;
    try {
      await postAction("/api/room/member", { action: "join", room_id: state.data.room_id, agent_id: agentId, route_profile_id: byId("seat-route").value, role_id: byId("seat-role").value, role_label: "" });
      toast(t("seat_added"));
    } catch (error) { toast(`${t("action_failed")}: ${error.message}`); }
    finally { button.disabled = false; }
  }

  async function removeSeat() {
    const button = byId("seat-remove"); const agentId = byId("seat-remove-member").value; if (!agentId || !state.data) { toast(t("no_removable_member")); return; }
    if (state.data.history_import?.selected) return toast(t("history_read_only"));
    button.disabled = true;
    try { await postAction("/api/room/member", { action: "leave", room_id: state.data.room_id, agent_id: agentId, route_profile_id: "", role_id: "equal-participant", role_label: "" }); toast(t("seat_removed")); }
    catch (error) { toast(`${t("action_failed")}: ${error.message}`); }
    finally { button.disabled = false; }
  }

  async function startManagedSession(event) {
    event.preventDefault();
    const permissionTier = byId("managed-permission").value;
    const approvalMode = state.preparedApprovalMode || (
      permissionTier === "full-development" ? "full-access"
        : permissionTier === "edit" ? "agent-delegated"
          : "approval-required"
    );
    const writeCapable = new Set(["edit", "full-development"]).has(permissionTier);
    const confirmationKey = permissionTier === "full-development" ? "full_access_session_confirm" : "write_session_confirm";
    if (writeCapable && !window.confirm(t(confirmationKey))) return;
    const button = byId("managed-start"); button.disabled = true; byId("managed-session-status").textContent = "…";
    try {
      const result = await postAction("/api/session/start", {
        agent_id: byId("managed-agent").value, role: byId("managed-role").value,
        permission_tier: byId("managed-permission").value,
        approval_mode: approvalMode,
        authorization_confirmed: writeCapable,
        governance_binding_id: byId("managed-binding-field").hidden ? "" : byId("managed-binding").value,
        requested_route: byId("managed-route").value.trim(), working_directory: byId("managed-directory").value.trim() || ".",
        input_text: byId("managed-input").value.trim(),
        attachments: state.managedAttachments.map(({ name, content_base64 }) => ({ name, content_base64 }))
      }, { timeoutMs: 120000 });
      const startedLabel = result.session_authorization?.mode === "once-per-session" ? t("session_authorized_once") : t("session_started");
      byId("managed-input").value = ""; state.managedAttachments = []; state.preparedApprovalMode = ""; renderManagedAttachments(); byId("managed-session-status").textContent = startedLabel; toast(startedLabel);
    } catch (error) { byId("managed-session-status").textContent = `${t("action_failed")}: ${error.message}`; toast(byId("managed-session-status").textContent); }
    finally { updateManagedPermissionControls(); }
  }

  async function refreshAgentCapabilities() {
    const button = byId("refresh-agent-capabilities");
    button.disabled = true;
    try {
      const result = await postAction("/api/agents/refresh", {});
      if (result.managed_agent_catalog && state.data) state.data.managed_agent_catalog = result.managed_agent_catalog;
      renderManagedControls();
      toast(t("capabilities_refreshed"));
    } catch (error) {
      toast(`${t("action_failed")}: ${error.message}`);
    } finally {
      button.disabled = false;
    }
  }

  async function enqueueWorkflow(event) {
    event.preventDefault(); const button = byId("workflow-enqueue"); button.disabled = true; byId("workflow-status").textContent = "…";
    try {
      await postAction("/api/workflow/enqueue", {
        workflow_id: byId("workflow-template").value, task_text: byId("workflow-task").value.trim(),
        max_attempts: Number(byId("workflow-attempts").value), timeout_seconds: Number(byId("workflow-timeout").value)
      });
      byId("workflow-task").value = ""; byId("workflow-status").textContent = t("workflow_enqueued"); toast(t("workflow_enqueued"));
    } catch (error) { byId("workflow-status").textContent = `${t("action_failed")}: ${error.message}`; toast(byId("workflow-status").textContent); }
    finally { button.disabled = !(state.data?.workflow_templates || []).length; }
  }

  async function submitGovernedForm(event, statusId, successKey, action) {
    event.preventDefault();
    const form = event.currentTarget; const status = byId(statusId); const button = form.querySelector('button[type="submit"]');
    if (button) button.disabled = true;
    status.textContent = "…";
    try {
      await action();
      status.textContent = t(successKey); toast(t(successKey));
    } catch (error) {
      status.textContent = `${t("action_failed")}: ${error.message}`; toast(status.textContent);
    } finally {
      if (button) button.disabled = false;
    }
  }

  function fillModelSelect(selectId, models, truncated = false) {
    const modelRows = Array.isArray(models) ? models : [];
    const boundedModels = [...new Set(modelRows.map((model) => String(model || "")).filter(Boolean))].slice(0, MAX_MODEL_OPTIONS);
    const select = byId(selectId);
    replaceSelectOptions(
      select,
      boundedModels.length ? boundedModels.map((model) => [model, model]) : [["", t("discover_models_first")]]
    );
    if (truncated || modelRows.length > boundedModels.length) {
      const marker = node("option", "", `… ${t("models_truncated")}`);
      marker.disabled = true;
      select.append(marker);
    }
  }

  async function discoverCcSwitchProviders() {
    const button = byId("ccswitch-discover"); button.disabled = true;
    byId("ccswitch-status").textContent = "…";
    try {
      const result = await postAction("/api/ccswitch/providers", { app: byId("ccswitch-app").value });
      state.ccswitchProviders = result.providers || [];
      replaceSelectOptions(
        byId("ccswitch-provider"),
        state.ccswitchProviders.length
          ? state.ccswitchProviders.map((entry) => [entry.provider_id, `${entry.current ? "● " : ""}${entry.name || entry.provider_id}`])
          : [["", t("no_records")]]
      );
      state.ccswitchModels = [];
      fillModelSelect("ccswitch-model", []);
      byId("ccswitch-status").textContent = t("providers_discovered");
      toast(t("providers_discovered"));
    } catch (error) { byId("ccswitch-status").textContent = `${t("action_failed")}: ${error.message}`; toast(byId("ccswitch-status").textContent); }
    finally { button.disabled = false; }
  }

  async function discoverCcSwitchModels() {
    const providerId = byId("ccswitch-provider").value;
    if (!providerId) return toast(t("discover_first"));
    const provider = state.ccswitchProviders.find((entry) => entry.provider_id === providerId);
    if (provider && !provider.has_endpoint) return toast(t("ccswitch_missing_endpoint"));
    const button = byId("ccswitch-models"); button.disabled = true;
    byId("ccswitch-status").textContent = "…";
    try {
      const result = await postAction("/api/ccswitch/models", { app: byId("ccswitch-app").value, provider_id: providerId }, { timeoutMs: 70000 });
      state.ccswitchModels = Array.isArray(result.models) ? result.models.slice(0, MAX_MODEL_OPTIONS) : [];
      fillModelSelect("ccswitch-model", state.ccswitchModels, Boolean(result.models_truncated));
      byId("ccswitch-status").textContent = result.models_truncated ? `${t("models_discovered")} · ${t("models_truncated")}` : t("models_discovered");
      toast(t("models_discovered"));
    } catch (error) { byId("ccswitch-status").textContent = `${t("action_failed")}: ${error.message}`; toast(byId("ccswitch-status").textContent); }
    finally { button.disabled = false; }
  }

  async function saveCcSwitchRoute(event) {
    return submitGovernedForm(event, "ccswitch-status", "route_saved", () => postAction("/api/ccswitch/route", {
      app: byId("ccswitch-app").value,
      provider_id: byId("ccswitch-provider").value,
      model_id: byId("ccswitch-model").value,
      agent_id: byId("ccswitch-agent").value.trim(),
      reasoning_mode: byId("ccswitch-reasoning").value
    }, { timeoutMs: 70000 }));
  }

  async function activateCcSwitchProvider() {
    const providerId = byId("ccswitch-provider").value;
    if (!providerId) return toast(t("discover_first"));
    const provider = state.ccswitchProviders.find((entry) => entry.provider_id === providerId);
    const confirmation = t("confirm_ccswitch_switch").replace("{app}", byId("ccswitch-app").selectedOptions[0]?.textContent || byId("ccswitch-app").value).replace("{provider}", provider?.name || providerId);
    if (!window.confirm(confirmation)) return;
    const button = byId("ccswitch-activate"); button.disabled = true;
    try {
      await postAction("/api/ccswitch/switch", { app: byId("ccswitch-app").value, provider_id: providerId, confirmed: true });
      byId("ccswitch-status").textContent = t("provider_activated"); toast(t("provider_activated"));
      await discoverCcSwitchProviders();
    } catch (error) { byId("ccswitch-status").textContent = `${t("action_failed")}: ${error.message}`; toast(byId("ccswitch-status").textContent); }
    finally { button.disabled = false; }
  }

  async function saveProviderConnection(event) {
    return submitGovernedForm(event, "provider-save-status", "provider_saved", async () => {
      await postAction("/api/provider/save", {
        connection_id: byId("provider-connection-id").value.trim(),
        display_name: byId("provider-display-name").value.trim(),
        route_class: byId("provider-route-class").value,
        endpoint: byId("provider-endpoint").value.trim(),
        api_key: byId("provider-route-class").value === "local" ? "" : byId("provider-api-key").value
      });
      byId("provider-api-key").value = "";
    });
  }

  async function discoverProviderModels() {
    const connectionId = byId("provider-route-connection").value;
    if (!connectionId) return toast(t("no_records"));
    const button = byId("provider-discover-models"); button.disabled = true;
    byId("provider-route-status").textContent = "…";
    try {
      const result = await postAction("/api/provider/discover", { connection_id: connectionId, timeout_seconds: 20 }, { timeoutMs: 30000 });
      state.providerModels = Array.isArray(result.models) ? result.models.slice(0, MAX_MODEL_OPTIONS) : [];
      fillModelSelect("provider-route-model", state.providerModels, Boolean(result.models_truncated));
      byId("provider-route-status").textContent = result.models_truncated ? `${t("models_discovered")} · ${t("models_truncated")}` : t("models_discovered"); toast(t("models_discovered"));
    } catch (error) { byId("provider-route-status").textContent = `${t("action_failed")}: ${error.message}`; toast(byId("provider-route-status").textContent); }
    finally { button.disabled = false; }
  }

  async function saveProviderRoute(event) {
    return submitGovernedForm(event, "provider-route-status", "route_saved", () => postAction("/api/provider/route", {
      connection_id: byId("provider-route-connection").value,
      agent_id: byId("provider-route-agent").value.trim(),
      model_id: byId("provider-route-model").value,
      reasoning_mode: byId("provider-route-reasoning").value,
      inference_timeout_seconds: 20
    }, { timeoutMs: 30000 }));
  }

  async function saveSchedule(event) {
    return submitGovernedForm(event, "schedule-status", "schedule_saved", () => postAction("/api/schedule/save", {
      schedule_id: byId("schedule-id").value.trim(),
      workflow_id: byId("schedule-workflow").value,
      task_text: byId("schedule-task").value.trim(),
      interval_minutes: Number(byId("schedule-interval").value),
      start_delay_minutes: Number(byId("schedule-delay").value),
      enabled: byId("schedule-enabled").checked,
      permission_decision_id: byId("schedule-permission").value.trim()
    }));
  }

  async function registerCapability(event) {
    return submitGovernedForm(event, "capability-status", "capability_registered", () => postAction("/api/capability/register", {
      capability_id: byId("capability-id").value.trim(),
      registry_version: byId("capability-version").value.trim(),
      kind: byId("capability-kind").value,
      display_name: byId("capability-name").value.trim(),
      source_sha256: byId("capability-sha").value.trim().toLowerCase(),
      sensitivity: byId("capability-sensitivity").value,
      enabled: true
    }));
  }

  async function grantCapability(event) {
    return submitGovernedForm(event, "capability-grant-status", "grant_saved", () => postAction("/api/capability/grant", {
      principal_type: byId("grant-principal-type").value,
      principal_id: byId("grant-principal-id").value.trim(),
      capability_id: byId("grant-capability-id").value.trim(),
      registry_version: byId("grant-capability-version").value.trim(),
      decision: byId("grant-decision").value,
      reason: byId("grant-reason").value.trim()
    }));
  }

  async function decidePermission(event) {
    return submitGovernedForm(event, "permission-status", "permission_saved", () => postAction("/api/permission/decide", {
      decision_id: byId("permission-id").value.trim(),
      task_id: byId("permission-task").value.trim(),
      agent_id: byId("permission-agent").value.trim(),
      decision: byId("permission-decision").value,
      reason: byId("permission-reason").value.trim(),
      ttl_hours: Number(byId("permission-ttl").value)
    }));
  }

  async function authorizeAgentIdentity(event) {
    event.preventDefault();
    const status = byId("identity-authorize-status"); const button = event.currentTarget.querySelector('button[type="submit"]'); button.disabled = true;
    status.textContent = "…";
    try {
      const result = await postAction("/api/identity/authorize", {
        agent_id: byId("identity-agent-id").value.trim(),
        profile: byId("identity-profile").value
      });
      byId("identity-decision-id").value = result.permission_decision_id || "";
      byId("identity-decision-field").hidden = false;
      status.textContent = t("identity_authorized");
      toast(t("identity_authorized"));
    } catch (error) {
      status.textContent = `${t("action_failed")}: ${error.message}`;
      toast(status.textContent);
    } finally { button.disabled = false; }
  }

  async function revokeAgentIdentity(event) {
    event.preventDefault();
    const status = byId("identity-revoke-status"); const button = event.currentTarget.querySelector('button[type="submit"]'); button.disabled = true;
    status.textContent = "…";
    try {
      await postAction("/api/identity/revoke", {
        capability_id: byId("identity-revoke-id").value.trim(),
        reason: byId("identity-revoke-reason").value.trim()
      });
      byId("identity-revoke-id").value = "";
      byId("identity-revoke-reason").value = "";
      status.textContent = t("identity_revoked");
      toast(t("identity_revoked"));
    } catch (error) {
      status.textContent = `${t("action_failed")}: ${error.message}`;
      toast(status.textContent);
    } finally { button.disabled = false; }
  }

  async function createExecution(event) {
    return submitGovernedForm(event, "execution-status", "execution_created", () => postAction("/api/execution/create", {
      binding_id: byId("execution-binding").value.trim(),
      task_id: byId("execution-task").value.trim(),
      agent_id: byId("execution-agent").value.trim(),
      permission_decision_id: byId("execution-permission").value.trim()
    }));
  }

  async function exportProof() {
    const button = byId("proof-export"); const status = byId("proof-status"); const taskId = byId("proof-task").value.trim();
    if (!taskId) { status.textContent = t("proof_required"); toast(status.textContent); return; }
    button.disabled = true; status.textContent = "…";
    try {
      const response = await postAction("/api/proof/export", { task_id: taskId });
      const relativePath = String(response?.result?.relative_path || "");
      if (relativePath.startsWith(".peerbridge-artifacts/proof-bundles/")) byId("proof-path").value = relativePath;
      status.textContent = t("proof_exported"); toast(t("proof_exported"));
    } catch (error) { status.textContent = `${t("action_failed")}: ${error.message}`; toast(status.textContent); }
    finally { button.disabled = false; }
  }

  async function verifyProof() {
    const button = byId("proof-verify"); const status = byId("proof-status"); const bundlePath = byId("proof-path").value.trim();
    if (!bundlePath) { status.textContent = t("proof_required"); toast(status.textContent); return; }
    button.disabled = true; status.textContent = "…";
    try {
      await postAction("/api/proof/verify", { bundle_path: bundlePath });
      status.textContent = t("proof_verified"); toast(t("proof_verified"));
    } catch (error) { status.textContent = `${t("action_failed")}: ${error.message}`; toast(status.textContent); }
    finally { button.disabled = false; }
  }

  function bind() {
    document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => {
      state.view = button.dataset.view;
      render();
      if (state.view === "change") fetchWorktreeDiff(false);
      document.querySelector(".content-view.active-view")?.scrollTo({ top: 0, left: 0, behavior: "auto" });
      closeMobilePanels();
    }));
    byId("chat-home").addEventListener("click", () => { state.view = "chat"; render(); });
    byId("announcement-button").addEventListener("click", () => { state.view = "announcement"; render(); });
    byId("room-search-button").addEventListener("click", () => { const popover = byId("room-search-popover"); popover.hidden = !popover.hidden; byId("room-search-button").setAttribute("aria-expanded", String(!popover.hidden)); if (!popover.hidden) byId("room-search-input").focus(); });
    byId("room-search-input").addEventListener("input", (event) => { state.roomSearch = event.target.value; renderRooms(); });
    byId("room-search-clear").addEventListener("click", () => { state.roomSearch = ""; byId("room-search-input").value = ""; renderRooms(); byId("room-search-input").focus(); });
    document.querySelectorAll("[data-inspector]").forEach((button) => button.addEventListener("click", () => { document.querySelectorAll("[data-inspector]").forEach((el) => el.classList.toggle("active", el === button)); document.querySelectorAll(".inspector-panel").forEach((el) => el.classList.toggle("active-panel", el.id === `${button.dataset.inspector}-panel`)); }));
    document.querySelectorAll("[data-cockpit-mode]").forEach((button) => button.addEventListener("click", () => {
      state.cockpitMode = button.dataset.cockpitMode;
      renderCockpit();
    }));
    document.querySelectorAll("[data-session-tab]").forEach((button) => button.addEventListener("click", () => {
      state.sessionDetailTab = button.dataset.sessionTab;
      renderCockpit();
    }));
    byId("composer").addEventListener("submit", sendMessage); byId("message-body").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); byId("composer").requestSubmit(); } });
    byId("recipient").addEventListener("change", updateComposerPermissionControls);
    byId("message-body").addEventListener("paste", (event) => handleClipboardImages(event, addAttachments));
    byId("attachment-button").addEventListener("click", () => byId("attachment-input").click());
    byId("attachment-input").addEventListener("change", (event) => addAttachments(event.target.files));
    byId("feedback-form").addEventListener("submit", submitFeedback);
    byId("feedback-attachment-button").addEventListener("click", () => byId("feedback-attachment-input").click());
    byId("feedback-attachment-input").addEventListener("change", (event) => addFeedbackAttachments(event.target.files));
    byId("feedback-message").addEventListener("paste", (event) => handleClipboardImages(event, addFeedbackAttachments));
    byId("feedback-credential-toggle").addEventListener("click", () => {
      const input = byId("feedback-credential"); const visible = input.type === "text"; input.type = visible ? "password" : "text"; byId("feedback-credential-toggle").textContent = t(visible ? "show" : "hide");
    });
    byId("new-room").addEventListener("click", () => { state.historyContinuationSourceRoom = ""; const dialog = byId("new-room-dialog"); if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", ""); byId("new-room-id").focus(); });
    byId("continue-history").addEventListener("click", startHistoryContinuation);
    byId("new-room-form").addEventListener("submit", createRoom);
    ["new-room-close", "new-room-cancel"].forEach((id) => byId(id).addEventListener("click", () => { state.historyContinuationSourceRoom = ""; byId("new-room-dialog").close(); }));
    byId("import-history").addEventListener("click", () => { const dialog = byId("history-dialog"); updateHistorySourceControls(); if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", ""); byId("history-provider").focus(); });
    byId("history-form").addEventListener("submit", importHistory);
    ["history-close", "history-cancel"].forEach((id) => byId(id).addEventListener("click", () => byId("history-dialog").close()));
    byId("appearance-button").addEventListener("click", openAppearanceDialog);
    byId("tutorial-button").addEventListener("click", openTutorial);
    byId("tutorial-close").addEventListener("click", () => closeTutorial(false));
    byId("tutorial-later").addEventListener("click", () => closeTutorial(false));
    byId("tutorial-done").addEventListener("click", () => closeTutorial(true));
    byId("appearance-form").addEventListener("submit", saveAppearance);
    ["appearance-close", "appearance-cancel"].forEach((id) => byId(id).addEventListener("click", () => byId("appearance-dialog").close()));
    byId("history-provider").addEventListener("change", updateHistorySourceControls);
    byId("native-history-discover").addEventListener("click", discoverNativeHistory);
    byId("history-file").addEventListener("change", () => {
      const file = byId("history-file").files?.[0];
      byId("history-file-status").textContent = file ? `${file.name} · ${compact(file.size)}B` : t("history_file_limit");
      byId("native-history-list").replaceChildren();
      byId("native-history-list").hidden = true;
      byId("history-import-status").textContent = file ? t("history_selection_required") : "";
    });
    byId("seat-add").addEventListener("click", addSeat); byId("seat-remove").addEventListener("click", removeSeat);
    byId("managed-session-form").addEventListener("submit", startManagedSession);
    byId("managed-input").addEventListener("paste", (event) => handleClipboardImages(event, addManagedAttachments));
    byId("managed-attachment-button").addEventListener("click", () => byId("managed-attachment-input").click());
    byId("managed-attachment-input").addEventListener("change", (event) => addManagedAttachments(event.target.files));
    byId("refresh-agent-capabilities").addEventListener("click", refreshAgentCapabilities);
    byId("workflow-form").addEventListener("submit", enqueueWorkflow);
    byId("schedule-form").addEventListener("submit", saveSchedule);
    byId("capability-form").addEventListener("submit", registerCapability);
    byId("capability-grant-form").addEventListener("submit", grantCapability);
    byId("identity-authorize-form").addEventListener("submit", authorizeAgentIdentity);
    byId("identity-revoke-form").addEventListener("submit", revokeAgentIdentity);
    byId("ccswitch-form").addEventListener("submit", saveCcSwitchRoute);
    byId("ccswitch-discover").addEventListener("click", discoverCcSwitchProviders);
    byId("ccswitch-models").addEventListener("click", discoverCcSwitchModels);
    byId("ccswitch-activate").addEventListener("click", activateCcSwitchProvider);
    byId("ccswitch-app").addEventListener("change", () => { state.ccswitchProviders = []; state.ccswitchModels = []; replaceSelectOptions(byId("ccswitch-provider"), [["", t("discover_first")]]); fillModelSelect("ccswitch-model", []); });
    byId("ccswitch-provider").addEventListener("change", () => { state.ccswitchModels = []; fillModelSelect("ccswitch-model", []); byId("ccswitch-status").textContent = ""; });
    byId("provider-connection-form").addEventListener("submit", saveProviderConnection);
    byId("provider-route-form").addEventListener("submit", saveProviderRoute);
    byId("provider-discover-models").addEventListener("click", discoverProviderModels);
    byId("provider-route-connection").addEventListener("change", () => { state.providerModels = []; fillModelSelect("provider-route-model", []); byId("provider-route-status").textContent = ""; });
    byId("provider-route-class").addEventListener("change", () => { const local = byId("provider-route-class").value === "local"; byId("provider-api-key").disabled = local; byId("provider-api-key").required = !local; });
    byId("provider-key-toggle").addEventListener("click", () => { const input = byId("provider-api-key"); const visible = input.type === "text"; input.type = visible ? "password" : "text"; byId("provider-key-toggle").textContent = t(visible ? "show" : "hide"); });
    byId("permission-form").addEventListener("submit", decidePermission);
    byId("execution-form").addEventListener("submit", createExecution);
    byId("proof-export").addEventListener("click", exportProof);
    byId("proof-verify").addEventListener("click", verifyProof);
    byId("workflow-attempts").addEventListener("input", () => { byId("workflow-attempts").dataset.touched = "true"; });
    byId("workflow-template").addEventListener("change", () => { delete byId("workflow-attempts").dataset.touched; renderWorkflowControls(); });
    byId("save-automation").addEventListener("click", async () => {
      const mode = byId("automation-mode").value; const maxRounds = Number(byId("automation-rounds").value); const maxMessages = Number(byId("automation-messages").value); const stagnation = Number(byId("automation-stagnation").value);
      try {
        await postAction("/api/room/automation", { room_id: state.data.room_id, mode, max_rounds: maxRounds, max_messages: maxMessages, stagnation_rounds: stagnation });
        byId("automation-menu").open = false; toast(t("automation_saved"));
      } catch (error) { toast(`${t("action_failed")}: ${error.message}`); }
    });
    document.querySelectorAll("[data-discussion-action]").forEach((button) => button.addEventListener("click", async () => {
      const discussionId = state.data?.automation?.active_discussion?.discussion_id; if (!discussionId) return;
      button.disabled = true;
      try { await postAction("/api/discussion/control", { discussion_id: discussionId, action: button.dataset.discussionAction, extra_rounds: 2 }); }
      catch (error) { toast(`${t("action_failed")}: ${error.message}`); }
      finally { button.disabled = false; }
    }));
    byId("refresh-button").addEventListener("click", () => fetchState(true)); byId("chat-focus-button").addEventListener("click", () => setChatFocus(!state.chatFocus)); byId("sidebar-toggle").addEventListener("click", () => byId("sidebar").classList.toggle("open")); byId("inspector-toggle").addEventListener("click", () => byId("inspector").classList.toggle("open")); byId("inspector-close").addEventListener("click", () => byId("inspector").classList.remove("open"));
    byId("refresh-announcements").addEventListener("click", refreshAnnouncements);
    byId("mark-announcements-read").addEventListener("click", markAnnouncementsRead);
    byId("audit-verify").addEventListener("click", verifyAuditChain);
    byId("worktree-diff-refresh").addEventListener("click", () => fetchWorktreeDiff(true));
    byId("start-review-workflow").addEventListener("click", openReviewWorkflow);
    bindPanelResize("sidebar-resizer", "sidebar", "--sidebar", 190, 320, 1); bindPanelResize("inspector-resizer", "inspector", "--inspector", 260, 420, -1);
    byId("locale-select").addEventListener("change", async (event) => {
      state.locale = event.target.value;
      localStorage.setItem("peerbridge.locale", state.locale);
      applyLocale();
      try {
        await postAction("/api/preferences/save", {
          locale: state.locale,
          tutorial_completed: Boolean(state.data?.appearance?.tutorial_completed)
        });
      } catch (error) {
        toast(`${t("action_failed")}: ${error.message}`);
      }
    });
    byId("load-older").addEventListener("click", async () => { const before = state.data?.page?.oldest_sequence; if (!before) return; try { const response = await fetch(`/api/bootstrap?room_id=${encodeURIComponent(state.roomId)}&before_sequence=${before}`, { headers: { Authorization: authorizationValue() }, cache: "no-store" }); if (!response.ok) throw new Error(String(response.status)); const payload = await response.json(); state.older = [...payload.messages, ...state.older]; state.data.page.has_older = payload.page.has_older; renderMessages(); } catch (error) { toast(error.message); } });
    document.addEventListener("keydown", (event) => { if (event.key !== "Escape") return; if (state.chatFocus) setChatFocus(false); const search = byId("room-search-popover"); if (!search.hidden) { search.hidden = true; byId("room-search-button").setAttribute("aria-expanded", "false"); } });
  }

  function start() {
    const params = new URLSearchParams(location.hash.slice(1));
    const fragmentToken = params.get("access_token") || "";
    if (fragmentToken) sessionStorage.setItem(workbenchSessionStorageKey, fragmentToken);
    state.token = fragmentToken || sessionStorage.getItem(workbenchSessionStorageKey) || "";
    const query = new URLSearchParams(location.search); state.roomId = query.get("room_id") || "lobby";
    history.replaceState(null, "", `${location.pathname}${location.search}`); restorePanelSizes(); bind(); applyLocale();
    if (!state.token) { byId("access-gate").hidden = false; return; }
    byId("app").hidden = false; fetchState(true); state.timer = window.setInterval(() => fetchState(false), 2500);
  }

  start();
})();

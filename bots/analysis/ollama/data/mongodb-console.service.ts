import { BadRequestException, ForbiddenException, Injectable, Logger, ServiceUnavailableException } from '@nestjs/common'
import { GoogleGenAI, Type } from '@google/genai'
import { GoogleAuth } from 'google-auth-library'
import { InjectConnection, InjectModel } from '@nestjs/mongoose'
import { ADMIN_GROUP, IListResponse } from '@sauna-net/dto'
import { Connection, Model } from 'mongoose'
import { MongoConst } from '../../constants/mongo'
import { AuthUser } from '../auth/auth.schema'
import { isAdministrationAllowedUser } from '../auth/auth.service'
import {
  MongodbConsoleAudit,
  MongodbConsoleAuditDocument,
  MongodbConsoleAuditStatus,
  MongodbConsoleConnectionKey,
  MongodbConsoleOperation,
} from './mongodb-console-audit.schema'

const DEFAULT_CONNECTION: MongodbConsoleConnectionKey = 'finsoft'
const DEFAULT_LIMIT = 100
const MAX_LIMIT = 500
const DEFAULT_MAX_TIME_MS = 15000
const MAX_MAX_TIME_MS = 60000
const VERTEX_AI_MAX_CHAT_OUTPUT_TOKENS = 1024
const VERTEX_AI_MAX_RETRIES = 2
const MAX_CHAT_HISTORY_ITEMS = 12
const DEFAULT_RAG_TOP_K = 4
const VERTEX_AI_MONGODB_AGENT_THINKING_BUDGET = 1024
const MAX_RAG_GROUNDING_ATTEMPTS = 3
const ALLOWED_OPERATIONS = new Set<MongodbConsoleOperation>(['find', 'findOne', 'aggregate', 'countDocuments', 'distinct'])
const ALLOWED_PIPELINE_STAGES = new Set([
  '$addFields',
  '$bucket',
  '$bucketAuto',
  '$count',
  '$facet',
  '$graphLookup',
  '$group',
  '$limit',
  '$lookup',
  '$match',
  '$project',
  '$replaceRoot',
  '$replaceWith',
  '$sample',
  '$set',
  '$skip',
  '$sort',
  '$sortByCount',
  '$unwind',
  '$unset',
])

type ExecuteMongoRequest = {
  query?: string
}

type MongodbChatMessage = {
  role?: 'user' | 'assistant'
  content?: string
  suggestedQuery?: string
}

type ChatMongoRequest = {
  message?: string
  currentQuery?: string
  history?: MongodbChatMessage[]
}

type ChatPromptContext = {
  message: string
  currentQuery: string
  history: MongodbChatMessage[]
}

type RagRetrievalPlan = {
  ragQueries: string[]
  explicitCollections: string[]
  retrievalGoal: string
}

type RetrievedRagContext = {
  sourceUri: string
  sourceDisplayName: string
  text: string
  score?: number
  query: string
}

type MongodbConsoleQuery = {
  connection?: MongodbConsoleConnectionKey
  collection?: string
  operation?: MongodbConsoleOperation
  filter?: Record<string, unknown>
  projection?: Record<string, unknown>
  sort?: Record<string, 1 | -1>
  limit?: number
  skip?: number
  pipeline?: Record<string, unknown>[]
  distinctField?: string
  maxTimeMs?: number
}

type ParsedMongoQuery = {
  connection: MongodbConsoleConnectionKey
  collection: string
  operation: MongodbConsoleOperation
  filter: Record<string, unknown>
  projection: Record<string, unknown>
  sort: Record<string, 1 | -1>
  limit: number
  skip: number
  pipeline: Record<string, unknown>[]
  distinctField?: string
  maxTimeMs: number
}

type ExecuteMongoResponse = {
  executedAt: string
  durationMs: number
  rowCount: number
  columns: string[]
  rows: Record<string, unknown>[]
  normalizedQuery: ParsedMongoQuery
}

type ChatMongoResponse = {
  answer: string
  suggestedQuery: string
  ragStatus: 'grounded' | 'not_grounded' | 'rag_disabled'
  ragSourceCount: number
  ragQueryAttempts: number
  usedModel: string
  usedLocation: string
  routingMode: 'direct' | 'hybrid_global' | 'hybrid_rag_fallback'
}

type GeminiGroundingInfo = {
  status: 'grounded' | 'not_grounded' | 'rag_disabled'
  sourceCount: number
}

type GeminiChatCompletion = {
  response: unknown
  grounding: GeminiGroundingInfo
  ragQueryAttempts: number
  usedModel: string
  usedLocation: string
  routingMode: 'direct' | 'hybrid_global' | 'hybrid_rag_fallback'
}

type VertexAiMongoAgentConfig = {
  project: string
  location: string
  model: string
  configuredModel: string
  credentialsPath: string
  ragCorpus: string
  ragTopK: number
  hasRag: boolean
  routingMode: 'direct' | 'hybrid_global' | 'hybrid_rag_fallback'
  disableHybridRouting: boolean
  isConfigured: boolean
}

type CollectionDescriptor = {
  connection: MongodbConsoleConnectionKey
  collections: string[]
}

@Injectable()
export class MongodbConsoleService {
  private readonly logger = new Logger(MongodbConsoleService.name)

  constructor(
    @InjectConnection(MongoConst.finsoftConnection)
    private readonly finsoftConnection: Connection,
    @InjectConnection(MongoConst.wholesaleConnection)
    private readonly wholesaleConnection: Connection,
    @InjectModel(MongodbConsoleAudit.name, MongoConst.finsoftConnection)
    private readonly auditLogs: Model<MongodbConsoleAuditDocument>,
  ) {}

  async execute(user: AuthUser, body: ExecuteMongoRequest): Promise<ExecuteMongoResponse> {
    this.ensureAdmin(user)

    if (typeof body?.query !== 'string' || !body.query.trim().length) {
      throw new BadRequestException('MongoDB dotaz je povinný a musí být ve formátu JSON')
    }

    const rawQuery = body.query.trim()
    let query: ParsedMongoQuery | null = null

    try {
      query = await this.parseAndValidateQuery(rawQuery)
    } catch (error: any) {
      await this.createAuditEntry(user, rawQuery, 'rejected', {
        connection: query?.connection || DEFAULT_CONNECTION,
        collection: query?.collection || '(unknown)',
        operation: query?.operation || 'find',
        errorMessage: error?.message || 'MongoDB dotaz nebyl povolen',
      })
      throw error
    }

    const startedAt = Date.now()

    try {
      const rows = await this.runQuery(query)
      const durationMs = Date.now() - startedAt
      const normalizedRows = rows.map(row => this.serializeDocument(row))
      const columns = this.collectColumns(normalizedRows)
      const response: ExecuteMongoResponse = {
        executedAt: new Date().toISOString(),
        durationMs,
        rowCount: normalizedRows.length,
        columns,
        rows: normalizedRows,
        normalizedQuery: query,
      }

      await this.createAuditEntry(user, rawQuery, 'succeeded', {
        connection: query.connection,
        collection: query.collection,
        operation: query.operation,
        durationMs,
        rowCount: normalizedRows.length,
        columnsCount: columns.length,
      })

      return response
    } catch (error: any) {
      const message = error?.message || 'Nepodařilo se spustit MongoDB dotaz'
      this.logger.warn(`Mongo console execution failed: ${message}`)
      await this.createAuditEntry(user, rawQuery, 'failed', {
        connection: query.connection,
        collection: query.collection,
        operation: query.operation,
        errorMessage: message,
      })
      throw new BadRequestException(message)
    }
  }

  async getAudit(user: AuthUser, page = 1, size = 20): Promise<IListResponse<MongodbConsoleAudit[]>> {
    this.ensureAdmin(user)

    const currentPage = Math.max(1, page || 1)
    const currentSize = Math.min(100, Math.max(1, size || 20))
    const total = await this.auditLogs.countDocuments({})
    const items = await this.auditLogs
      .find({})
      .sort({ createdAt: -1 })
      .skip((currentPage - 1) * currentSize)
      .limit(currentSize)
      .lean()

    return {
      total,
      items,
    }
  }

  async listCollections(user: AuthUser): Promise<CollectionDescriptor[]> {
    this.ensureAdmin(user)

    return Promise.all([
      this.getCollectionsForConnection('finsoft'),
      this.getCollectionsForConnection('wholesale'),
    ])
  }

  async chat(user: AuthUser, body: ChatMongoRequest): Promise<ChatMongoResponse> {
    this.ensureAdmin(user)

    const message = `${body?.message || ''}`.trim()
    if (!message) {
      throw new BadRequestException('Dotaz pro Gemini je povinný')
    }

    const vertexConfig = this.getVertexAiMongoAgentConfig()

    if (vertexConfig.hasRag && !this.getRagCorpusLocation(vertexConfig.ragCorpus)) {
      throw new ServiceUnavailableException('VERTEX_AI_MONGODB_AGENT_RAG_CORPUS má neplatný formát. Očekávaný tvar je projects/{PROJECT_ID}/locations/{REGION}/ragCorpora/{CORPUS_ID}.')
    }

    if (!vertexConfig.isConfigured) {
      throw new ServiceUnavailableException('Gemini MongoDB asistent není na serveru nakonfigurovaný.')
    }

    const chatContext: ChatPromptContext = {
      message,
      currentQuery: `${body?.currentQuery || ''}`,
      history: Array.isArray(body?.history) ? body.history : [],
    }
    const completion = await this.generateChatCompletion(chatContext, vertexConfig)

    return this.parseChatResponse(completion, chatContext)
  }

  private async parseAndValidateQuery(rawQuery: string): Promise<ParsedMongoQuery> {
    let parsed: MongodbConsoleQuery

    try {
      parsed = JSON.parse(rawQuery)
    } catch {
      throw new BadRequestException('MongoDB editor očekává validní JSON objekt')
    }

    if (!this.isPlainObject(parsed)) {
      throw new BadRequestException('Kořen MongoDB dotazu musí být JSON objekt')
    }

    const connection = parsed.connection === 'wholesale' ? 'wholesale' : DEFAULT_CONNECTION
    const operation = ALLOWED_OPERATIONS.has(parsed.operation as MongodbConsoleOperation)
      ? (parsed.operation as MongodbConsoleOperation)
      : 'find'
    const collection = `${parsed.collection || ''}`.trim()

    if (!collection) {
      throw new BadRequestException('Pole collection je povinné')
    }

    const knownCollections = await this.getCollectionsForConnection(connection)
    if (!knownCollections.collections.includes(collection)) {
      throw new BadRequestException(`Kolekce ${collection} na connection ${connection} neexistuje nebo není dostupná`)
    }

    const filter = this.normalizePlainObject(parsed.filter, 'filter')
    const projection = this.normalizePlainObject(parsed.projection, 'projection')
    const sort = this.normalizeSort(parsed.sort)
    const limit = this.normalizeInteger(parsed.limit, DEFAULT_LIMIT, 0, MAX_LIMIT)
    const skip = this.normalizeInteger(parsed.skip, 0, 0, 100000)
    const maxTimeMs = this.normalizeInteger(parsed.maxTimeMs, DEFAULT_MAX_TIME_MS, 1000, MAX_MAX_TIME_MS)
    const pipeline = this.normalizePipeline(parsed.pipeline)
    const distinctField = `${parsed.distinctField || ''}`.trim() || undefined

    if (operation === 'aggregate' && !pipeline.length) {
      throw new BadRequestException('Pro operation aggregate je povinné nenulové pole pipeline')
    }

    if (operation === 'distinct' && !distinctField) {
      throw new BadRequestException('Pro operation distinct je povinné pole distinctField')
    }

    if (operation !== 'aggregate' && parsed.pipeline) {
      throw new BadRequestException('Pole pipeline lze použít jen pro operation aggregate')
    }

    return {
      connection,
      collection,
      operation,
      filter,
      projection,
      sort,
      limit,
      skip,
      pipeline,
      distinctField,
      maxTimeMs,
    }
  }

  private async runQuery(query: ParsedMongoQuery): Promise<Record<string, unknown>[]> {
    const collection = this.getNativeCollection(query.connection, query.collection)

    switch (query.operation) {
      case 'find': {
        const cursor = collection.find(query.filter, {
          projection: Object.keys(query.projection).length ? query.projection : undefined,
          maxTimeMS: query.maxTimeMs,
        })

        if (Object.keys(query.sort).length) {
          cursor.sort(query.sort)
        }

        cursor.skip(query.skip)
        cursor.limit(query.limit)
        return cursor.toArray() as Promise<Record<string, unknown>[]>
      }

      case 'findOne': {
        const row = await collection.findOne(query.filter, {
          projection: Object.keys(query.projection).length ? query.projection : undefined,
          sort: Object.keys(query.sort).length ? query.sort : undefined,
          maxTimeMS: query.maxTimeMs,
        })
        return row ? [row as Record<string, unknown>] : []
      }

      case 'countDocuments': {
        const count = await collection.countDocuments(query.filter, {
          maxTimeMS: query.maxTimeMs,
        })
        return [{ count }]
      }

      case 'distinct': {
        const values = await collection.distinct(query.distinctField || '', query.filter, {
          maxTimeMS: query.maxTimeMs,
        })
        return values.map((value, index) => ({ index, value }))
      }

      case 'aggregate': {
        const pipeline = [...query.pipeline]
        if (query.skip > 0) {
          pipeline.push({ $skip: query.skip })
        }
        if (query.limit > 0) {
          pipeline.push({ $limit: query.limit })
        }

        return collection.aggregate(pipeline, { maxTimeMS: query.maxTimeMs }).toArray() as Promise<Record<string, unknown>[]>
      }
    }
  }

  private getChatHistoryLines(payload: ChatPromptContext): string[] {
    return payload.history
      .slice(-MAX_CHAT_HISTORY_ITEMS)
      .map((entry, index) => {
        const role = entry?.role === 'assistant' ? 'Asistent' : 'Uživatel'
        const content = this.cleanPromptText(entry?.content)
        const suggestedQuery = this.cleanPromptText(entry?.suggestedQuery)

        return [
          `#${index + 1} ${role}: ${content || '(prázdné)'}`,
          suggestedQuery ? `Navržený MongoDB JSON dotaz: ${suggestedQuery}` : '',
        ].filter(Boolean).join('\n')
      })
  }

  private buildDirectChatPrompt(payload: ChatPromptContext): string {
    const historyLines = this.getChatHistoryLines(payload)
    const currentQuery = this.cleanPromptText(payload.currentQuery)

    return [
      'Jsi Gemini Vertex AI agent, seniorní specialista na MongoDB data explorer pro Harvii a FinSoft.',
      'Pomáháš pouze s read-only MongoDB dotazy pro interní admin konzoli /administration/mongodb.',
      'Konzole nepoužívá Mongo shell. Navrhuješ pouze JSON DSL se strukturou connection, collection, operation, filter, projection, sort, limit, skip, pipeline, distinctField, maxTimeMs.',
      'Povolené operation jsou jen find, findOne, aggregate, countDocuments a distinct.',
      'Nikdy nenavrhuj write operace, update pipeline, insert, delete, replace, findOneAndUpdate, bulkWrite, createIndex ani pipeline stage $out nebo $merge.',
      'Odpovídej česky, stručně, konkrétně a technicky přesně.',
      'RAG podklady nejsou pro tuto odpověď k dispozici. Jasně přiznej, když si nejsi jistá názvem kolekce nebo pole.',
      'Když není známé schéma, navrhni bezpečný průzkumný find s malým limitem a úzkou projekcí nebo bez projekce.',
      'Pokud se nehodí navrhnout žádný dotaz, vrať suggestedQuery jako prázdný řetězec.',
      'Vracíš výhradně validní JSON bez markdownu a bez dalších poznámek.',
      'JSON musí mít přesně klíče: answer, suggestedQuery.',
      currentQuery ? `Aktuální obsah editoru MongoDB: ${currentQuery}` : 'Aktuální obsah editoru MongoDB: (prázdný)',
      historyLines.length ? 'Dosavadní konverzace:' : 'Dosavadní konverzace: (žádná)',
      ...(historyLines.length ? historyLines : []),
      `Aktuální uživatelský dotaz: ${this.cleanPromptText(payload.message)}`,
    ].join('\n\n')
  }

  private buildRetrievalPlannerPrompt(payload: ChatPromptContext): string {
    const historyLines = this.getChatHistoryLines(payload)
    const currentQuery = this.cleanPromptText(payload.currentQuery)

    return [
      'Jsi plánovač retrieval dotazů pro Harvia MongoDB asistenta.',
      'Tvůj jediný úkol je z uživatelského dotazu připravit robustní dotazy pro Vertex RAG retrieval.',
      'Neodpovídáš uživateli a negeneruješ finální Mongo JSON.',
      'Vrať nejvýše 3 retrieval dotazy. První má být nejpřesnější technický dotaz, další 2 mají být synonymní nebo zkratkové varianty.',
      'Retrieval dotazy musí být krátké, věcné a zaměřené na collection names, field names, nested fields, business domény a read-only JSON DSL.',
      'Pokud uživatel explicitně uvede collection, zachovej ji ve všech relevantních retrieval dotazech.',
      'Vracíš výhradně validní JSON bez markdownu a bez dalších poznámek.',
      'JSON musí mít přesně klíče: retrievalGoal, ragQueries, explicitCollections.',
      currentQuery ? `Aktuální obsah editoru MongoDB: ${currentQuery}` : 'Aktuální obsah editoru MongoDB: (prázdný)',
      historyLines.length ? 'Dosavadní konverzace:' : 'Dosavadní konverzace: (žádná)',
      ...(historyLines.length ? historyLines : []),
      `Aktuální uživatelský dotaz: ${this.cleanPromptText(payload.message)}`,
    ].join('\n\n')
  }

  private buildSynthesisPrompt(payload: ChatPromptContext, plan: RagRetrievalPlan, contexts: RetrievedRagContext[]): string {
    const historyLines = this.getChatHistoryLines(payload)
    const currentQuery = this.cleanPromptText(payload.currentQuery)
    const contextLines = contexts.map((context, index) => {
      return [
        `#${index + 1} Zdroj: ${context.sourceDisplayName || '(bez názvu)'}`,
        context.sourceUri ? `URI: ${context.sourceUri}` : '',
        context.score !== undefined ? `Skóre: ${context.score}` : '',
        `Retrieval dotaz: ${context.query}`,
        `Obsah: ${this.cleanPromptText(context.text)}`,
      ].filter(Boolean).join('\n')
    })

    return [
      'Jsi Gemini Vertex AI agent, seniorní specialista na MongoDB data explorer pro Harvii a FinSoft.',
      'Pomáháš pouze s read-only MongoDB dotazy pro interní admin konzoli /administration/mongodb.',
      'Navrhuješ pouze JSON DSL ve formátu stringu pro admin editor.',
      'Níže dostaneš pouze serverem explicitně vyhledané RAG podklady. Použij jen je jako zdroj pravdy pro názvy kolekcí, pole, vnořená pole a business kontext.',
      'Nikdy si nevymýšlej názvy kolekcí ani pole, která nejsou doložená v dodaných podkladech.',
      'Když podklady nestačí, přiznej nejistotu a vrať jen bezpečný průzkumný query nebo prázdný suggestedQuery.',
      'Povolené operation jsou pouze find, findOne, aggregate, countDocuments a distinct.',
      'Nikdy nenavrhuj mutace ani write pipeline stage $out a $merge.',
      'SuggestedQuery musí být validní JSON string vhodný k vložení do editoru a musí používat jen povolené operation.',
      'Vracíš výhradně validní JSON bez markdownu a bez dalších poznámek.',
      'JSON musí mít přesně klíče: answer, suggestedQuery.',
      `Retrieval cíl: ${plan.retrievalGoal || 'neuvedeno'}`,
      plan.ragQueries.length ? `Použité retrieval dotazy: ${plan.ragQueries.join(' | ')}` : 'Použité retrieval dotazy: (žádné)',
      plan.explicitCollections.length ? `Explicitní kolekce: ${plan.explicitCollections.join(', ')}` : 'Explicitní kolekce: (žádné)',
      currentQuery ? `Aktuální obsah editoru MongoDB: ${currentQuery}` : 'Aktuální obsah editoru MongoDB: (prázdný)',
      historyLines.length ? 'Dosavadní konverzace:' : 'Dosavadní konverzace: (žádná)',
      ...(historyLines.length ? historyLines : []),
      contextLines.length ? 'Serverem vyhledané RAG podklady:' : 'Serverem vyhledané RAG podklady: (žádné)',
      ...(contextLines.length ? contextLines : []),
      `Aktuální uživatelský dotaz: ${this.cleanPromptText(payload.message)}`,
    ].join('\n\n')
  }

  private cleanPromptText(value?: string): string {
    return `${value || ''}`
      .replace(/\r/g, ' ')
      .replace(/\u0000/g, ' ')
      .replace(/\s+/g, ' ')
      .trim()
  }

  private async generateChatCompletion(
    chatContext: ChatPromptContext,
    vertexConfig: VertexAiMongoAgentConfig,
  ): Promise<GeminiChatCompletion> {
    if (!vertexConfig.hasRag) {
      const completion = await this.callGemini(
        this.buildDirectChatPrompt(chatContext),
        vertexConfig,
        'mongodbAssistantChat',
        this.getGeminiChatResponseSchema(),
      )
      return {
        response: completion.response,
        grounding: {
          status: 'rag_disabled',
          sourceCount: 0,
        },
        ragQueryAttempts: 0,
        usedModel: completion.usedModel,
        usedLocation: vertexConfig.location,
        routingMode: vertexConfig.routingMode,
      }
    }

    const retrievalPlan = await this.generateRetrievalPlan(chatContext, vertexConfig)
    const retrievalResult = await this.retrieveRagContexts(retrievalPlan, vertexConfig)
    const synthesisPrompt = this.buildSynthesisPrompt(chatContext, retrievalPlan, retrievalResult.contexts)
    const completion = await this.callGemini(
      synthesisPrompt,
      vertexConfig,
      'mongodbAssistantChat',
      this.getGeminiChatResponseSchema(),
    )

    return {
      response: completion.response,
      grounding: {
        status: retrievalResult.contexts.length > 0 ? 'grounded' : 'not_grounded',
        sourceCount: retrievalResult.contexts.length,
      },
      ragQueryAttempts: retrievalResult.attempts,
      usedModel: completion.usedModel,
      usedLocation: vertexConfig.location,
      routingMode: vertexConfig.routingMode,
    }
  }

  private async callGemini(
    prompt: string,
    vertexConfig: VertexAiMongoAgentConfig,
    operation: 'mongodbAssistantChat' | 'mongodbAssistantPlanner',
    responseSchema: Record<string, unknown>,
  ): Promise<{ response: unknown; usedModel: string }> {
    const ai = new GoogleGenAI({
      vertexai: true,
      project: vertexConfig.project,
      location: vertexConfig.location,
      apiVersion: 'v1',
      googleAuthOptions: {
        keyFilename: vertexConfig.credentialsPath,
      },
    })
    const modelCandidates = this.getVertexModelCandidates(vertexConfig.model)

    for (let modelIndex = 0; modelIndex < modelCandidates.length; modelIndex += 1) {
      const modelName = modelCandidates[modelIndex]

      for (let attempt = 1; attempt <= VERTEX_AI_MAX_RETRIES + 1; attempt += 1) {
        try {
          const response = await ai.models.generateContent({
            model: modelName,
            contents: prompt,
            config: {
              temperature: 0.2,
              maxOutputTokens: VERTEX_AI_MAX_CHAT_OUTPUT_TOKENS,
              responseMimeType: 'application/json',
              responseSchema,
              thinkingConfig: {
                thinkingBudget: VERTEX_AI_MONGODB_AGENT_THINKING_BUDGET,
                includeThoughts: false,
              },
            },
          })

          return {
            response,
            usedModel: modelName,
          }
        } catch (error) {
          const normalizedError = this.normalizeVertexAiError(error)
          const retryDelayMs = this.getGeminiRetryDelayMs(normalizedError.status, normalizedError.retryAfterHeader, attempt)
          const isLastAttemptForModel = attempt > VERTEX_AI_MAX_RETRIES || retryDelayMs === null
          const hasModelFallback = modelIndex < modelCandidates.length - 1

          if (!isLastAttemptForModel) {
            await this.wait(retryDelayMs)
            continue
          }

          if (!hasModelFallback) {
            throw new ServiceUnavailableException(this.getGeminiErrorMessage(normalizedError.status, normalizedError.responseSnippet, vertexConfig))
          }

          break
        }
      }
    }

    throw new ServiceUnavailableException('Gemini MongoDB asistent neočekávaně vyčerpal všechny pokusy.')
  }

  private async generateRetrievalPlan(
    chatContext: ChatPromptContext,
    vertexConfig: VertexAiMongoAgentConfig,
  ): Promise<RagRetrievalPlan> {
    const plannerResponse = await this.callGemini(
      this.buildRetrievalPlannerPrompt(chatContext),
      vertexConfig,
      'mongodbAssistantPlanner',
      this.getGeminiRetrievalPlannerSchema(),
    )
    const parsed = this.parseJsonObject(this.extractGeminiText(plannerResponse.response))
    const fallbackCollections = await this.extractExplicitCollectionNames([
      chatContext.message,
      chatContext.currentQuery,
      ...chatContext.history.map(entry => entry?.content || ''),
      ...chatContext.history.map(entry => entry?.suggestedQuery || ''),
    ])
    const explicitCollections = await this.normalizeExplicitCollections(parsed.explicitCollections, fallbackCollections)
    const ragQueries = this.normalizeRagQueries(parsed.ragQueries, chatContext, explicitCollections)

    return {
      retrievalGoal: this.cleanPromptText(`${parsed.retrievalGoal || chatContext.message}`) || chatContext.message,
      ragQueries,
      explicitCollections,
    }
  }

  private async normalizeExplicitCollections(candidate: unknown, fallbackCollections: string[]): Promise<string[]> {
    const knownCollections = await this.getKnownCollectionNames()
    const matches = new Set<string>()

    for (const value of Array.isArray(candidate) ? candidate : []) {
      const normalized = this.normalizeCollectionReference(`${value || ''}`, knownCollections)
      if (normalized) {
        matches.add(normalized)
      }
    }

    for (const collection of fallbackCollections) {
      if (collection) {
        matches.add(collection)
      }
    }

    return [...matches]
  }

  private normalizeRagQueries(candidate: unknown, chatContext: ChatPromptContext, explicitCollections: string[]): string[] {
    const queries = new Set<string>()

    for (const value of Array.isArray(candidate) ? candidate : []) {
      const normalized = this.cleanPromptText(`${value || ''}`)
      if (normalized) {
        queries.add(normalized)
      }
    }

    if (!queries.size) {
      queries.add(this.cleanPromptText(chatContext.message))
    }

    if (explicitCollections.length) {
      const firstCollection = explicitCollections[0]
      queries.add(`${firstCollection} collection fields nested fields schema`)
      queries.add(`${firstCollection} mongodb collection business context`)
    }

    return [...queries]
      .map(query => this.cleanPromptText(query))
      .filter(Boolean)
      .slice(0, MAX_RAG_GROUNDING_ATTEMPTS)
  }

  private async retrieveRagContexts(
    plan: RagRetrievalPlan,
    vertexConfig: VertexAiMongoAgentConfig,
  ): Promise<{ contexts: RetrievedRagContext[]; attempts: number }> {
    const contexts = new Map<string, RetrievedRagContext>()
    let attempts = 0

    for (const query of plan.ragQueries.slice(0, MAX_RAG_GROUNDING_ATTEMPTS)) {
      attempts += 1
      const retrievedContexts = await this.callRetrieveContexts(query, vertexConfig)

      for (const context of retrievedContexts) {
        const key = `${context.sourceUri}::${context.text}`
        if (!contexts.has(key)) {
          contexts.set(key, context)
        }
      }

      if (contexts.size >= vertexConfig.ragTopK) {
        break
      }
    }

    return {
      contexts: [...contexts.values()].slice(0, Math.max(vertexConfig.ragTopK, 1)),
      attempts,
    }
  }

  private async callRetrieveContexts(query: string, vertexConfig: VertexAiMongoAgentConfig): Promise<RetrievedRagContext[]> {
    const client = await this.getGoogleAuthClient(vertexConfig.credentialsPath)
    const url = `https://${vertexConfig.location}-aiplatform.googleapis.com/v1/projects/${vertexConfig.project}/locations/${vertexConfig.location}:retrieveContexts`
    const data = {
      vertexRagStore: {
        ragResources: [
          {
            ragCorpus: vertexConfig.ragCorpus,
          },
        ],
      },
      query: {
        text: query,
        ragRetrievalConfig: {
          topK: vertexConfig.ragTopK,
        },
      },
    }

    for (let attempt = 1; attempt <= VERTEX_AI_MAX_RETRIES + 1; attempt += 1) {
      try {
        const response = await client.request<{
          contexts?: {
            contexts?: Array<{
              sourceUri?: string
              sourceDisplayName?: string
              source_uri?: string
              source_display_name?: string
              text?: string
              score?: number
            }>
          }
        }>({
          url,
          method: 'POST',
          data,
          headers: {
            'Content-Type': 'application/json; charset=utf-8',
          },
        })

        const responseContexts = Array.isArray(response.data?.contexts?.contexts)
          ? response.data.contexts.contexts
          : []

        return responseContexts
          .map(context => ({
            sourceUri: `${context?.sourceUri || context?.source_uri || ''}`.trim(),
            sourceDisplayName: `${context?.sourceDisplayName || context?.source_display_name || ''}`.trim(),
            text: `${context?.text || ''}`.trim(),
            score: typeof context?.score === 'number' ? context.score : undefined,
            query,
          }))
          .filter(context => !!context.text)
      } catch (error) {
        const normalizedError = this.normalizeVertexAiError(error)
        const retryDelayMs = this.getGeminiRetryDelayMs(normalizedError.status, normalizedError.retryAfterHeader, attempt)
        const isLastAttempt = attempt > VERTEX_AI_MAX_RETRIES || retryDelayMs === null

        if (!isLastAttempt) {
          await this.wait(retryDelayMs)
          continue
        }

        this.logger.error(
          `Vertex RAG retrieveContexts failed for query "${this.truncateForLog(query, 120)}" with status ${normalizedError.status || 'N/A'}: ${normalizedError.responseSnippet || 'Unknown error'}`,
        )

        throw new ServiceUnavailableException(this.getRagRetrievalErrorMessage(normalizedError.status, normalizedError.responseSnippet))
      }
    }

    throw new ServiceUnavailableException('Vertex RAG retrieval neočekávaně vyčerpal všechny pokusy.')
  }

  private async getGoogleAuthClient(credentialsPath: string) {
    const auth = new GoogleAuth({
      keyFilename: credentialsPath,
      scopes: ['https://www.googleapis.com/auth/cloud-platform'],
    })

    return auth.getClient()
  }

  private async parseChatResponse(completion: GeminiChatCompletion, chatContext: ChatPromptContext): Promise<ChatMongoResponse> {
    const rawText = this.extractGeminiText(completion.response)
    let parsed: Record<string, unknown> | null = null
    const malformedResponse = this.extractMalformedStructuredChatResponse(rawText)

    try {
      parsed = this.parseJsonObject(rawText)
    } catch (error) {
      if (!(error instanceof ServiceUnavailableException)) {
        throw error
      }

      if (malformedResponse.answer || malformedResponse.suggestedQuery) {
        parsed = malformedResponse
        this.logger.warn(`Gemini Mongo chat response was not valid JSON. Salvaged structured fields from malformed response. Raw preview: ${this.truncateForLog(rawText, 240)}`)
      } else {
        this.logger.warn(`Gemini Mongo chat response was not valid JSON. Falling back to plain-text answer. Raw preview: ${this.truncateForLog(rawText, 240)}`)
      }
    }

    let answer = this.cleanPromptText(typeof parsed?.answer === 'string' ? parsed.answer : '')
    let suggestedQuery = typeof parsed?.suggestedQuery === 'string' ? parsed.suggestedQuery.trim() : ''

    if (!parsed) {
      answer = this.appendAnswerNote(
        this.cleanPromptText(malformedResponse.answer || rawText),
        'Odpověď od asistenta přišla v nestandardním formátu, proto tentokrát neposílám automaticky vložitelný MongoDB dotaz.',
      )
      suggestedQuery = ''
    }

    if (!answer) {
      throw new ServiceUnavailableException('Gemini nevrátila odpověď pro MongoDB asistenta.')
    }

    if (suggestedQuery) {
      try {
        await this.parseAndValidateQuery(suggestedQuery)
      } catch {
        suggestedQuery = ''
        answer = this.appendAnswerNote(answer, 'Navržený MongoDB JSON jsem skryla, protože neprošel serverovou kontrolou read-only pravidel.')
      }
    }

    if (completion.grounding.status === 'not_grounded') {
      const fallbackQuery = await this.resolveExploratorySuggestedQuery(chatContext, suggestedQuery)

      if (fallbackQuery) {
        suggestedQuery = fallbackQuery
        answer = this.appendAnswerNote(answer, `V RAG podkladech jsem nenašla dostatečnou oporu ani po ${completion.ragQueryAttempts} pokusech hledání, proto posílám jen bezpečný průzkumný MongoDB dotaz nad explicitně uvedenou kolekcí.`)
      } else {
        suggestedQuery = ''
        answer = this.appendAnswerNote(answer, `V RAG podkladech jsem nenašla dostatečnou oporu ani po ${completion.ragQueryAttempts} pokusech hledání, proto neposílám finální MongoDB dotaz.`)
      }
    }

    return {
      answer,
      suggestedQuery,
      ragStatus: completion.grounding.status,
      ragSourceCount: completion.grounding.sourceCount,
      ragQueryAttempts: completion.ragQueryAttempts,
      usedModel: completion.usedModel,
      usedLocation: completion.usedLocation,
      routingMode: completion.routingMode,
    }
  }

  private appendAnswerNote(answer: string, note: string): string {
    if (!note) {
      return answer
    }

    return answer.includes(note) ? answer : `${answer} ${note}`.trim()
  }

  private extractMalformedStructuredChatResponse(candidateText: string): Pick<ChatMongoResponse, 'answer' | 'suggestedQuery'> {
    const answer = this.extractLooseFieldValue(candidateText, 'answer') || this.cleanPromptText(candidateText.replace(/^[\s{]+/, '').replace(/[}\s]+$/, ''))
    const suggestedQuery = this.extractLooseFieldValue(candidateText, 'suggestedQuery')

    return {
      answer,
      suggestedQuery,
    }
  }

  private extractJsonStringField(candidateText: string, fieldName: string): string {
    const fieldPattern = new RegExp(`"${fieldName}"\\s*:\\s*("(?:\\\\.|[^"\\\\])*")`, 'i')
    const match = candidateText.match(fieldPattern)
    if (!match?.[1]) {
      return ''
    }

    try {
      return this.cleanPromptText(JSON.parse(match[1]) as string)
    } catch {
      return ''
    }
  }

  private extractLooseFieldValue(candidateText: string, fieldName: string): string {
    const fieldPattern = new RegExp(`"${fieldName}"\\s*:\\s*([\\s\\S]*?)(?:,\\s*"[A-Za-z0-9_]+"\\s*:|\\s*})`, 'i')
    const match = candidateText.match(fieldPattern)
    const rawValue = match?.[1]?.trim()

    if (!rawValue) {
      return ''
    }

    if (rawValue.startsWith('"')) {
      return this.cleanPromptText(rawValue.replace(/^"/, '').replace(/"$/, ''))
    }

    return this.cleanPromptText(rawValue)
  }

  private async resolveExploratorySuggestedQuery(chatContext: ChatPromptContext, suggestedQuery: string): Promise<string> {
    const explicitCollections = await this.extractExplicitCollectionNames([
      chatContext.message,
      chatContext.currentQuery,
      ...chatContext.history.map(entry => entry?.content || ''),
      ...chatContext.history.map(entry => entry?.suggestedQuery || ''),
    ])

    if (suggestedQuery && await this.isSafeExploratoryQuery(suggestedQuery, explicitCollections)) {
      return suggestedQuery.trim()
    }

    const [firstCollection] = explicitCollections
    if (!firstCollection) {
      return ''
    }

    const connection = await this.resolveCollectionConnection(firstCollection, chatContext.currentQuery)
    return JSON.stringify({
      connection,
      collection: firstCollection,
      operation: 'find',
      filter: {},
      projection: {},
      sort: { _id: -1 },
      limit: this.extractRequestedLimit(chatContext.message),
    }, null, 2)
  }

  private async isSafeExploratoryQuery(query: string, allowedCollections: string[]): Promise<boolean> {
    try {
      const parsed = await this.parseAndValidateQuery(query)
      return !!parsed.collection && allowedCollections.includes(parsed.collection)
    } catch {
      return false
    }
  }

  private async extractExplicitCollectionNames(values: string[]): Promise<string[]> {
    const knownCollections = await this.getKnownCollectionNames()
    const matches = new Set<string>()

    for (const value of values) {
      const text = `${value || ''}`
      for (const collection of knownCollections) {
        const pattern = new RegExp(`(^|[^\\w-])${this.escapeRegExp(collection)}([^\\w-]|$)`, 'i')
        if (pattern.test(text)) {
          matches.add(collection)
        }
      }

      const collectionLabelMatches = text.matchAll(/\b(?:kolekc(?:e|i)|collection)\s+([a-z0-9_\-]+)/gi)
      for (const match of collectionLabelMatches) {
        const normalized = this.normalizeCollectionReference(match[1] || '', knownCollections)
        if (normalized) {
          matches.add(normalized)
        }
      }
    }

    return [...matches]
  }

  private normalizeCollectionReference(value: string, knownCollections: string[]): string {
    const normalized = `${value || ''}`
      .replace(/["'`,;:\[\]{}()]/g, '')
      .trim()
      .toLowerCase()

    if (!normalized) {
      return ''
    }

    return knownCollections.find(collection => collection.toLowerCase() === normalized) || ''
  }

  private extractRequestedLimit(message: string): number {
    const topMatch = `${message || ''}`.match(/\b(\d{1,3})\b/)
    const parsed = Number.parseInt(topMatch?.[1] || '', 10)

    if (!Number.isFinite(parsed) || parsed <= 0) {
      return 20
    }

    return Math.min(parsed, 100)
  }

  private extractGeminiText(response: unknown): string {
    const directText = (response as { text?: string })?.text?.trim()
    if (directText) {
      return directText
    }

    const candidateText = (response as {
      candidates?: Array<{ content?: { parts?: Array<{ text?: string }> } }>
    })?.candidates?.[0]?.content?.parts
      ?.map(part => part?.text || '')
      .join('')
      .trim()

    if (!candidateText) {
      throw new ServiceUnavailableException('Gemini nevrátila žádný obsah pro MongoDB asistenta.')
    }

    return candidateText
  }

  private parseJsonObject(candidateText: string): Record<string, unknown> {
    const normalized = candidateText
      .replace(/^```json\s*/i, '')
      .replace(/^```/i, '')
      .replace(/```$/i, '')
      .trim()

    try {
      return JSON.parse(normalized) as Record<string, unknown>
    } catch {
      const objectMatch = normalized.match(/\{[\s\S]*\}/)

      if (!objectMatch) {
        throw new ServiceUnavailableException('Gemini nevrátila validní JSON pro MongoDB asistenta.')
      }

      try {
        return JSON.parse(objectMatch[0]) as Record<string, unknown>
      } catch {
        throw new ServiceUnavailableException('Gemini nevrátila validní JSON pro MongoDB asistenta.')
      }
    }
  }

  private getGeminiRetrievalPlannerSchema(): Record<string, unknown> {
    return {
      type: Type.OBJECT,
      description: 'Plán retrieval dotazů pro Vertex RAG.',
      required: ['retrievalGoal', 'ragQueries', 'explicitCollections'],
      propertyOrdering: ['retrievalGoal', 'ragQueries', 'explicitCollections'],
      properties: {
        retrievalGoal: {
          type: Type.STRING,
          description: 'Krátké technické shrnutí, co se má v RAG dohledat.',
        },
        ragQueries: {
          type: Type.ARRAY,
          description: 'Seznam nejvýše tří retrieval dotazů.',
          items: {
            type: Type.STRING,
          },
        },
        explicitCollections: {
          type: Type.ARRAY,
          description: 'Seznam explicitně zmíněných nebo potvrzených kolekcí.',
          items: {
            type: Type.STRING,
          },
        },
      },
    }
  }

  private getGeminiChatResponseSchema(): Record<string, unknown> {
    return {
      type: Type.OBJECT,
      description: 'Odpověď MongoDB asistenta pro Harvia administraci.',
      required: ['answer', 'suggestedQuery'],
      propertyOrdering: ['answer', 'suggestedQuery'],
      properties: {
        answer: {
          type: Type.STRING,
          description: 'Stručná česká odpověď pro uživatele bez markdownu.',
        },
        suggestedQuery: {
          type: Type.STRING,
          description: 'Read-only MongoDB JSON dotaz vhodný pro vložení do editoru nebo prázdný řetězec.',
        },
      },
    }
  }

  private getVertexAiMongoAgentConfig(): VertexAiMongoAgentConfig {
    const project = process.env.VERTEX_AI_PROJECT_ID?.trim() || ''
    const location = process.env.VERTEX_AI_REGION?.trim() || ''
    const mongoModel = process.env.VERTEX_AI_MONGODB_AGENT_MODEL?.trim() || ''
    const mongoCredentialsPath = process.env.VERTEX_AI_MONGODB_AGENT_CREDENTIALS?.trim() || ''
    const mongoRagCorpus = process.env.VERTEX_AI_MONGODB_AGENT_RAG_CORPUS?.trim() || ''
    const mongoRagTopK = process.env.VERTEX_AI_MONGODB_AGENT_RAG_TOP_K || ''
    const mongoDisableHybridRouting = process.env.VERTEX_AI_MONGODB_AGENT_DISABLE_HYBRID_ROUTING || ''
    const hasMongoSpecificOverrides = [
      mongoModel,
      mongoCredentialsPath,
      mongoRagCorpus,
      mongoRagTopK,
      mongoDisableHybridRouting,
    ].some(Boolean)

    const configuredModel = hasMongoSpecificOverrides
      ? (mongoModel || process.env.VERTEX_AI_SQL_AGENT_MODEL?.trim() || process.env.VERTEX_AI_MODEL?.trim() || '')
      : (process.env.VERTEX_AI_SQL_AGENT_MODEL?.trim() || process.env.VERTEX_AI_MODEL?.trim() || '')
    const credentialsPath = hasMongoSpecificOverrides
      ? (mongoCredentialsPath || process.env.VERTEX_AI_SQL_AGENT_CREDENTIALS?.trim() || '')
      : (process.env.VERTEX_AI_SQL_AGENT_CREDENTIALS?.trim() || '')
    const ragCorpus = hasMongoSpecificOverrides
      ? (mongoRagCorpus || process.env.VERTEX_AI_SQL_AGENT_RAG_CORPUS?.trim() || '')
      : (process.env.VERTEX_AI_SQL_AGENT_RAG_CORPUS?.trim() || '')
    const ragTopKValue = Number.parseInt(
      hasMongoSpecificOverrides
        ? (mongoRagTopK || process.env.VERTEX_AI_SQL_AGENT_RAG_TOP_K || '')
        : (process.env.VERTEX_AI_SQL_AGENT_RAG_TOP_K || ''),
      10,
    )
    const ragTopK = Number.isFinite(ragTopKValue) && ragTopKValue > 0 ? ragTopKValue : DEFAULT_RAG_TOP_K
    const hasRag = !!ragCorpus
    const ragCorpusLocation = this.getRagCorpusLocation(ragCorpus)
    const disableHybridRouting = this.isTruthyEnv(
      hasMongoSpecificOverrides
        ? (mongoDisableHybridRouting || process.env.VERTEX_AI_SQL_AGENT_DISABLE_HYBRID_ROUTING)
        : process.env.VERTEX_AI_SQL_AGENT_DISABLE_HYBRID_ROUTING,
    )

    let model = configuredModel
    let resolvedLocation = location
    let routingMode: 'direct' | 'hybrid_global' | 'hybrid_rag_fallback' = 'direct'

    if (!disableHybridRouting && this.isGemini31PreviewModel(configuredModel)) {
      if (hasRag) {
        model = 'gemini-2.5-pro'
        resolvedLocation = ragCorpusLocation || (location === 'global' ? '' : location)
        routingMode = 'hybrid_rag_fallback'
      } else {
        resolvedLocation = 'global'
        routingMode = 'hybrid_global'
      }
    }

    return {
      project,
      location: resolvedLocation,
      model,
      configuredModel,
      credentialsPath,
      ragCorpus,
      ragTopK,
      hasRag,
      routingMode,
      disableHybridRouting,
      isConfigured: !!project && !!resolvedLocation && !!model && !!credentialsPath,
    }
  }

  private isTruthyEnv(value?: string): boolean {
    return ['1', 'true', 'yes', 'on'].includes(`${value || ''}`.trim().toLowerCase())
  }

  private isGemini31PreviewModel(model: string): boolean {
    return model === 'gemini-3.1-pro-preview' || model === 'gemini-3.1-pro-preview-customtools'
  }

  private getRagCorpusLocation(ragCorpus: string): string {
    const match = ragCorpus.match(/^projects\/[^/]+\/locations\/([^/]+)\/ragCorpora\/[^/]+$/)
    return match?.[1]?.trim() || ''
  }

  private getVertexModelCandidates(configuredModel: string): string[] {
    const normalizedModel = configuredModel.trim()
    if (!normalizedModel) {
      return []
    }

    if (normalizedModel === 'gemini-3.1-pro-preview' || normalizedModel === 'gemini-3.1-pro-preview-customtools') {
      return ['gemini-3.1-pro-preview', 'gemini-3.1-pro-preview-customtools', 'gemini-2.5-pro', 'gemini-2.5-flash']
    }

    if (normalizedModel === 'gemini-2.5-pro' || normalizedModel === 'gemini-2.5-pro-001') {
      return ['gemini-2.5-pro', 'gemini-2.5-pro-001', 'gemini-2.5-flash']
    }

    if (normalizedModel === 'gemini-2.0-flash-001' || normalizedModel === 'gemini-2.0-flash') {
      return ['gemini-2.0-flash-001', 'gemini-2.0-flash', 'gemini-2.5-flash']
    }

    if (normalizedModel === 'gemini-2.0-flash-lite-001' || normalizedModel === 'gemini-2.0-flash-lite') {
      return ['gemini-2.0-flash-lite-001', 'gemini-2.0-flash-lite', 'gemini-2.5-flash-lite']
    }

    return [normalizedModel]
  }

  private getGeminiErrorMessage(
    status: number | undefined,
    responseText: string,
    vertexConfig?: { model?: string; location?: string; hasRag?: boolean },
  ): string {
    const normalizedResponse = responseText.toLowerCase()

    if (status === 429 || normalizedResponse.includes('resource_exhausted')) {
      return 'Gemini je momentálně dočasně nedostupná kvůli vyčerpanému limitu nebo příliš častým požadavkům. Zkuste akci prosím za chvíli znovu.'
    }

    if ((status || 0) >= 500) {
      return 'Gemini momentálně neodpovídá korektně. Zkuste akci prosím za chvíli znovu.'
    }

    if (!status) {
      return 'Vertex AI Gemini request selhal bez dostupného HTTP statusu. Zkontrolujte prosím service account a Vertex AI konfiguraci.'
    }

    if (status === 404) {
      if ((vertexConfig?.model || '').startsWith('gemini-3.1-pro-preview') && (vertexConfig?.location || '') !== 'global') {
        return 'Vertex AI vrátila 404, protože gemini-3.1-pro-preview je dostupný jen přes region global, ale Mongo agent je aktuálně nastavený na jiný region. Pro tento model nastavte VERTEX_AI_REGION=global, nebo použijte regionální model jako gemini-2.5-pro či gemini-2.5-flash.'
      }

      if (normalizedResponse.includes('ragcorpora') || normalizedResponse.includes('vertexragstore') || normalizedResponse.includes('retrieval')) {
        return 'Vertex AI vrátila 404 pro RAG resource. Zkontrolujte prosím VERTEX_AI_MONGODB_AGENT_RAG_CORPUS, region a oprávnění service accountu k Vertex RAG Engine.'
      }

      if (normalizedResponse.includes('publishers/google/models') || normalizedResponse.includes('model')) {
        return 'Vertex AI vrátila 404 pro zvolený model. Zkontrolujte prosím VERTEX_AI_MONGODB_AGENT_MODEL, region a dostupnost Gemini modelu pro tento projekt.'
      }

      return 'Vertex AI vrátila 404. Nejčastěji jde o nedostupný model nebo neexistující RAG corpus v daném regionu/projektu.'
    }

    return `Gemini API vrátila chybu ${status}.`
  }

  private getRagRetrievalErrorMessage(status: number | undefined, responseText: string): string {
    const normalizedResponse = responseText.toLowerCase()

    if (status === 429 || normalizedResponse.includes('resource_exhausted')) {
      return 'Vertex RAG retrieval je momentálně dočasně nedostupný kvůli limitu nebo příliš častým požadavkům. Zkuste akci prosím za chvíli znovu.'
    }

    if ((status || 0) >= 500) {
      return 'Vertex RAG retrieval momentálně neodpovídá korektně. Zkuste akci prosím za chvíli znovu.'
    }

    if (status === 404 || normalizedResponse.includes('ragcorpora') || normalizedResponse.includes('retrievecontexts')) {
      return 'Vertex RAG retrieval nenašel zadaný corpus nebo není dostupný v daném regionu. Zkontrolujte prosím VERTEX_AI_MONGODB_AGENT_RAG_CORPUS, region a oprávnění service accountu.'
    }

    if (!status) {
      return `Vertex RAG retrieval selhal ještě před HTTP odpovědí. Nejčastěji jde o neplatnou cestu ke credentials, problém s tokenem service accountu, DNS/TLS chybu nebo síťové spojení. Detail: ${this.truncateForLog(responseText || 'bez detailu', 220)}`
    }

    return `Vertex RAG retrieval vrátil chybu ${status}.`
  }

  private getGeminiRetryDelayMs(status: number | undefined, retryAfterHeader: string | null, attempt: number): number | null {
    if (status !== 429 && (status || 0) < 500) {
      return null
    }

    if (retryAfterHeader) {
      const retryAfterSeconds = Number.parseInt(retryAfterHeader, 10)
      if (Number.isFinite(retryAfterSeconds) && retryAfterSeconds > 0) {
        return retryAfterSeconds * 1000
      }
    }

    const baseDelayMs = status === 429 ? 2000 : 1000
    return baseDelayMs * Math.pow(2, attempt - 1)
  }

  private wait(delayMs: number): Promise<void> {
    return new Promise(resolve => setTimeout(resolve, delayMs))
  }

  private normalizeVertexAiError(error: unknown): {
    status?: number
    statusText?: string
    retryAfterHeader: string | null
    requestId?: string
    responseSnippet: string
  } {
    const candidate = error as {
      status?: number
      code?: number | string
      message?: string
      details?: unknown
      response?: {
        status?: number
        statusText?: string
        headers?: Record<string, string>
        data?: unknown
      }
    }

    const status = candidate.status
      || candidate.response?.status
      || (typeof candidate.code === 'number' ? candidate.code : Number.parseInt(`${candidate.code || ''}`, 10) || undefined)
    const responseText = typeof candidate.response?.data === 'string'
      ? candidate.response.data
      : candidate.response?.data
        ? JSON.stringify(candidate.response.data)
        : `${candidate.message || candidate.details || 'Unknown Vertex AI error'}`
    const headers = candidate.response?.headers || {}

    return {
      status,
      statusText: candidate.response?.statusText,
      retryAfterHeader: headers['retry-after'] || null,
      requestId: headers['x-request-id'] || headers['x-goog-request-id'] || undefined,
      responseSnippet: this.truncateForLog(responseText, 500),
    }
  }

  private truncateForLog(value: string, maxLength: number): string {
    if (!value) {
      return ''
    }

    return value.length > maxLength ? `${value.slice(0, maxLength)}...` : value
  }

  private async getKnownCollectionNames(): Promise<string[]> {
    const [finsoftCollections, wholesaleCollections] = await Promise.all([
      this.getCollectionsForConnection('finsoft'),
      this.getCollectionsForConnection('wholesale'),
    ])

    return [...new Set([...finsoftCollections.collections, ...wholesaleCollections.collections])]
  }

  private async resolveCollectionConnection(collection: string, currentQuery?: string): Promise<MongodbConsoleConnectionKey> {
    const [finsoftCollections, wholesaleCollections] = await Promise.all([
      this.getCollectionsForConnection('finsoft'),
      this.getCollectionsForConnection('wholesale'),
    ])

    const existsInFinsoft = finsoftCollections.collections.includes(collection)
    const existsInWholesale = wholesaleCollections.collections.includes(collection)

    if (existsInFinsoft && !existsInWholesale) {
      return 'finsoft'
    }

    if (existsInWholesale && !existsInFinsoft) {
      return 'wholesale'
    }

    try {
      if (currentQuery?.trim()) {
        const parsed = await this.parseAndValidateQuery(currentQuery)
        return parsed.connection
      }
    } catch {
      return DEFAULT_CONNECTION
    }

    return DEFAULT_CONNECTION
  }

  private escapeRegExp(value: string): string {
    return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  }

  private async getCollectionsForConnection(connection: MongodbConsoleConnectionKey): Promise<CollectionDescriptor> {
    const db = this.getConnection(connection).db

    if (!db) {
      throw new BadRequestException(`Mongo connection ${connection} není připravená`)
    }

    const collections = await db.listCollections({}, { nameOnly: true }).toArray()

    return {
      connection,
      collections: collections
        .map(item => `${item.name || ''}`)
        .filter(Boolean)
        .sort((left, right) => left.localeCompare(right)),
    }
  }

  private getNativeCollection(connection: MongodbConsoleConnectionKey, collection: string) {
    const db = this.getConnection(connection).db
    if (!db) {
      throw new BadRequestException(`Mongo connection ${connection} není připravená`)
    }

    return db.collection(collection)
  }

  private getConnection(connection: MongodbConsoleConnectionKey): Connection {
    return connection === 'wholesale' ? this.wholesaleConnection : this.finsoftConnection
  }

  private normalizePlainObject(value: unknown, fieldName: string): Record<string, unknown> {
    if (typeof value === 'undefined' || value === null) {
      return {}
    }

    if (!this.isPlainObject(value)) {
      throw new BadRequestException(`Pole ${fieldName} musí být JSON objekt`)
    }

    return value as Record<string, unknown>
  }

  private normalizeSort(value: unknown): Record<string, 1 | -1> {
    if (typeof value === 'undefined' || value === null) {
      return {}
    }

    if (!this.isPlainObject(value)) {
      throw new BadRequestException('Pole sort musí být JSON objekt')
    }

    return Object.entries(value as Record<string, unknown>).reduce<Record<string, 1 | -1>>((acc, [key, entryValue]) => {
      if (entryValue === 1 || entryValue === '1' || entryValue === 'asc' || entryValue === 'ASC') {
        acc[key] = 1
        return acc
      }

      if (entryValue === -1 || entryValue === '-1' || entryValue === 'desc' || entryValue === 'DESC') {
        acc[key] = -1
        return acc
      }

      throw new BadRequestException(`Sort pro pole ${key} musí být 1 nebo -1`)
    }, {})
  }

  private normalizePipeline(value: unknown): Record<string, unknown>[] {
    if (typeof value === 'undefined' || value === null) {
      return []
    }

    if (!Array.isArray(value)) {
      throw new BadRequestException('Pole pipeline musí být pole stage objektů')
    }

    return value.map((stage, index) => {
      if (!this.isPlainObject(stage)) {
        throw new BadRequestException(`Pipeline stage na indexu ${index} musí být JSON objekt`)
      }

      const entries = Object.entries(stage)
      if (entries.length !== 1) {
        throw new BadRequestException(`Pipeline stage na indexu ${index} musí obsahovat právě jeden operátor`)
      }

      const [operator] = entries[0]
      if (!ALLOWED_PIPELINE_STAGES.has(operator)) {
        throw new BadRequestException(`Pipeline stage ${operator} není v admin konzoli povolená`)
      }

      return stage as Record<string, unknown>
    })
  }

  private normalizeInteger(value: unknown, defaultValue: number, min: number, max: number): number {
    if (typeof value === 'undefined' || value === null || value === '') {
      return defaultValue
    }

    const numericValue = Number(value)
    if (!Number.isFinite(numericValue)) {
      throw new BadRequestException('Číselná pole dotazu musí obsahovat validní číslo')
    }

    return Math.min(max, Math.max(min, Math.trunc(numericValue)))
  }

  private serializeDocument(value: unknown): any {
    if (value === null || typeof value === 'undefined') {
      return value
    }

    if (value instanceof Date) {
      return value.toISOString()
    }

    if (Array.isArray(value)) {
      return value.map(item => this.serializeDocument(item))
    }

    if (typeof value === 'object') {
      if (typeof (value as any).toHexString === 'function') {
        return (value as any).toHexString()
      }

      if (typeof (value as any).toString === 'function' && (value as any)._bsontype) {
        return (value as any).toString()
      }

      return Object.entries(value as Record<string, unknown>).reduce<Record<string, unknown>>((acc, [key, nestedValue]) => {
        acc[key] = this.serializeDocument(nestedValue)
        return acc
      }, {})
    }

    return value
  }

  private collectColumns(rows: Record<string, unknown>[]): string[] {
    return Array.from(
      rows.reduce<Set<string>>((acc, row) => {
        Object.keys(row || {}).forEach(key => acc.add(key))
        return acc
      }, new Set<string>()),
    ).sort((left, right) => left.localeCompare(right))
  }

  private isPlainObject(value: unknown): boolean {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      return false
    }

    const prototype = Object.getPrototypeOf(value)
    return prototype === Object.prototype || prototype === null
  }

  private async createAuditEntry(
    user: AuthUser,
    query: string,
    status: MongodbConsoleAuditStatus,
    payload: {
      connection: MongodbConsoleConnectionKey
      collection: string
      operation: MongodbConsoleOperation
      durationMs?: number
      rowCount?: number
      columnsCount?: number
      errorMessage?: string
    },
  ) {
    return this.auditLogs.create({
      actorUserId: user.user_id,
      actorUserName: user.user_name,
      actorFullname: user.fullname,
      status,
      query,
      queryPreview: this.getQueryPreview(query),
      connection: payload.connection,
      collection: payload.collection,
      operation: payload.operation,
      durationMs: payload.durationMs,
      rowCount: payload.rowCount,
      columnsCount: payload.columnsCount,
      errorMessage: payload.errorMessage,
    })
  }

  private getQueryPreview(query: string): string {
    return `${query || ''}`.replace(/\s+/g, ' ').trim().slice(0, 180)
  }

  private ensureAdmin(user: AuthUser) {
    const hasGroup = !!user.groups?.find(group => group.id === ADMIN_GROUP)

    if (!isAdministrationAllowedUser(user.user_id) && !hasGroup) {
      throw new ForbiddenException('Na tuto akci nemáte oprávnění')
    }
  }
}
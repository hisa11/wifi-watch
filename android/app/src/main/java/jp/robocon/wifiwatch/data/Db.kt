package jp.robocon.wifiwatch.data

import android.content.Context
import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Room
import androidx.room.RoomDatabase
import kotlinx.coroutines.flow.Flow

/** 自分の歩測の記録（端末が正本を持ち、PCへ送る）。json は中継に送る record そのもの。 */
@Entity(tableName = "walks")
data class WalkEntity(
    @PrimaryKey val id: String,
    val createdAt: Double,
    val json: String,
    val location: String?,
    val nStations: Int,
    val sentAt: Long? = null,
    val lastError: String? = null,
)

/** 中継から読んだ記録の写し（一覧の情報と、開いたことのある本文）。PCが正本。 */
@Entity(tableName = "records")
data class RecordEntity(
    @PrimaryKey val id: String,
    val kind: String,
    val createdAt: Double,
    val metaJson: String,
    val bodyJson: String? = null,
    val updatedAt: Long,
    /** アプリで変えてまだPCの写しに反映されていないラベル（JSON）。反映されたら消す */
    val pendingLabelJson: String? = null,
    val pendingEditedAt: Double? = null,
)

/** 送る待ち行列（ラベルの変更と歩測の記録）。要求IDで冪等なので二重に届いても結果は同じ。 */
@Entity(tableName = "outbox")
data class OutboxEntity(
    @PrimaryKey val requestId: String,
    val kind: String, // label / walk
    val refId: String,
    val bodyJson: String,
    val createdAt: Long,
    val attempts: Int = 0,
    val lastError: String? = null,
)

@Dao
interface WalkDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun put(w: WalkEntity)

    @Query("SELECT * FROM walks ORDER BY createdAt DESC")
    fun all(): Flow<List<WalkEntity>>

    @Query("SELECT * FROM walks ORDER BY createdAt")
    suspend fun list(): List<WalkEntity>

    @Query("UPDATE walks SET sentAt = :at, lastError = NULL WHERE id = :id")
    suspend fun markSent(id: String, at: Long)

    @Query("UPDATE walks SET lastError = :err WHERE id = :id")
    suspend fun markError(id: String, err: String)
}

@Dao
interface RecordDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun put(r: RecordEntity)

    @Query("SELECT * FROM records WHERE id = :id")
    suspend fun get(id: String): RecordEntity?

    @Query("SELECT * FROM records WHERE id = :id")
    fun watch(id: String): Flow<RecordEntity?>

    @Query("SELECT * FROM records ORDER BY createdAt DESC")
    fun all(): Flow<List<RecordEntity>>

    @Query("SELECT MAX(updatedAt) FROM records")
    suspend fun maxUpdated(): Long?

    @Query("DELETE FROM records WHERE id = :id")
    suspend fun delete(id: String)
}

@Dao
interface OutboxDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun put(o: OutboxEntity)

    @Query("SELECT * FROM outbox ORDER BY createdAt LIMIT 50")
    suspend fun pending(): List<OutboxEntity>

    @Query("SELECT COUNT(*) FROM outbox")
    fun count(): Flow<Int>

    @Query("SELECT COUNT(*) FROM outbox")
    suspend fun size(): Int

    @Query("DELETE FROM outbox WHERE requestId = :id")
    suspend fun delete(id: String)

    @Query("UPDATE outbox SET attempts = attempts + 1, lastError = :err WHERE requestId = :id")
    suspend fun failed(id: String, err: String)
}

@Database(entities = [WalkEntity::class, RecordEntity::class, OutboxEntity::class], version = 1, exportSchema = true)
abstract class AppDb : RoomDatabase() {
    abstract fun walks(): WalkDao
    abstract fun records(): RecordDao
    abstract fun outbox(): OutboxDao

    companion object {
        fun open(context: Context): AppDb =
            Room.databaseBuilder(context, AppDb::class.java, "wifiwatch.db").fallbackToDestructiveMigration(false).build()
    }
}
